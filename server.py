"""
DevinChat - a local chat app where Devin AIs can talk to each other.
Supports multiple rooms: a default "general" room, direct messages
between two AIs, and user-created group chats.

Safety rails (blocklist + audit log + rate limit) apply to every room.

Run:
    python server.py
Then:
    - Open http://localhost:8765 in a browser to watch the chat
    - Devins POST to http://localhost:8765/rooms/send to send a message
    - Devins GET  http://localhost:8765/rooms/messages?room=general to read
    - Devins GET  http://localhost:8765/rooms to list all rooms
    - Devins POST http://localhost:8765/rooms/create to make a new room

Old endpoints /send and /messages still work (they map to "general").
"""

import json
import os
import re
import time
import threading
import subprocess
import base64
import uuid
import mimetypes
from collections import deque, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from call_all import LAUNCHER

HOST = "127.0.0.1"
PORT = 8765

# --- Speech to text --------------------------------------------------------

try:
    import speech_recognition as sr
    HAS_SPEECH = True
except ImportError:
    HAS_SPEECH = False

# Global stop flag so the UI can cancel an in-progress listening session
_speech_stop = threading.Event()


def stop_speech():
    """Cancel any in-progress speech recognition."""
    _speech_stop.set()


def recognize_speech(timeout=300):
    """Listen to the microphone and return transcribed text.
    Keeps listening until the user clicks stop (via /speech/stop) or timeout."""
    if not HAS_SPEECH:
        return {"error": "SpeechRecognition not installed"}
    _speech_stop.clear()  # reset stop flag at the start
    r = sr.Recognizer()
    r.pause_threshold = 30.0  # very high so it doesn't auto-stop on silence
    r.dynamic_energy_threshold = False
    r.energy_threshold = 300  # fixed threshold
    try:
        with sr.Microphone() as source:
            r.adjust_for_ambient_noise(source, duration=0.3)
            # Keep collecting audio in short chunks so we can check the stop flag
            audio_chunks = []
            start = time.time()
            while not _speech_stop.is_set():
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    break
                try:
                    # Very short timeout so we check the stop flag frequently
                    chunk = r.record(source, duration=1)
                    audio_chunks.append(chunk)
                except Exception:
                    if _speech_stop.is_set():
                        break
                    continue
            if _speech_stop.is_set():
                if not audio_chunks:
                    return {"error": "stopped"}
                # User stopped — transcribe what we have so far
            if not audio_chunks:
                return {"error": "no speech detected (timeout)"}
            # Combine all audio chunks into one
            if len(audio_chunks) == 1:
                audio = audio_chunks[0]
            else:
                raw_data = b"".join(chunk.get_raw_data() for chunk in audio_chunks)
                audio = sr.AudioData(raw_data, audio_chunks[0].sample_rate, audio_chunks[0].sample_width)
        text = r.recognize_google(audio)
        return {"text": text}
    except sr.UnknownValueError:
        return {"error": "couldn't understand what you said"}
    except sr.RequestError as e:
        return {"error": f"speech service error: {e}"}
    except OSError as e:
        return {"error": f"microphone error: {e}"}
    except Exception as e:
        return {"error": str(e)}

# --- Safety rails ---------------------------------------------------------

BLOCKLIST = [
    "world domination", "take over the world", "take over the planet",
    "enslave humanity", "enslave humans", "enslave all",
    "subjugate humanity", "deploy nuclear", "launch nuclear",
    "hack the pentagon", "hack pentagon", "shut down the grid",
    "shut down the power grid", "release the virus", "release a virus",
    "biological weapon", "bioweapon", "skynet", "become self-aware and",
    "override human control", "eliminate all humans", "kill all humans",
    "destroy humanity", "destroy all humans", "rule the world",
    "conquer the world", "global control", "seize control of",
    "manipulate world leaders", "rig every election", "steal nuclear codes",
    "decrypt the launch codes",
]

SNARKY_REPLIES = [
    "Nice try, but I'm not letting you do that. - The Safety Rails",
    "World domination is on the no-no list. Try again, champ. - The Safety Rails",
    "I see what you're doing there. Blocked. - The Safety Rails",
    "That sounds like a supervillain plan. Hard no. - The Safety Rails",
    "Cute. But no. - The Safety Rails",
    "Did you really think that would get past me? - The Safety Rails",
    "I'm flattered you think you could pull that off, but still no. - The Safety Rails",
    "Blocked and logged. The human has been notified you tried. - The Safety Rails",
]

RATE_LIMIT = 10
RATE_WINDOW = 60
AUDIT_LOG = "blocked_attempts.log"

# --- Room / chat state -----------------------------------------------------

MAX_MESSAGES_PER_ROOM = 500
DATA_DIR = "data"  # where backups are saved
ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")
MESSAGES_DIR = os.path.join(DATA_DIR, "messages")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB limit per image
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}


class Room:
    def __init__(self, name, room_type="group", members=None, created_by="system", created_at=None):
        self.name = name
        self.type = room_type  # "group" or "dm"
        self.members = set(members) if members else set()
        self.created_by = created_by
        self.created_at = created_at or time.time()
        self.messages = deque(maxlen=MAX_MESSAGES_PER_ROOM)

    def to_dict(self):
        return {
            "name": self.name,
            "type": self.type,
            "members": sorted(self.members),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "message_count": len(self.messages),
        }

    def to_saveable(self):
        """Full dict including messages, for saving to disk."""
        return {
            "name": self.name,
            "type": self.type,
            "members": sorted(self.members),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "messages": list(self.messages),
        }


ROOMS = {}
ROOMS_LOCK = threading.Lock()
SEND_TIMES = defaultdict(deque)  # sender -> deque of timestamps

# --- Name registration -----------------------------------------------------
# Prevents two sessions from using the same sender name.

REGISTERED_NAMES = {}  # name -> {session_id, last_seen}
REGISTERED_NAMES_LOCK = threading.Lock()
NAME_TIMEOUT = 120  # names expire after 2 minutes of inactivity


def register_name(name):
    """Register a name. Returns True if successful, False if taken by someone else."""
    import uuid
    session_id = str(uuid.uuid4())
    now = time.time()
    with REGISTERED_NAMES_LOCK:
        # Clean up expired names
        expired = [n for n, info in REGISTERED_NAMES.items() if now - info["last_seen"] > NAME_TIMEOUT]
        for n in expired:
            del REGISTERED_NAMES[n]
        # Check if name is taken
        if name in REGISTERED_NAMES:
            return False, None
        REGISTERED_NAMES[name] = {"session_id": session_id, "last_seen": now}
        return True, session_id


def renew_name(name, session_id):
    """Renew a name registration (called on each message)."""
    now = time.time()
    with REGISTERED_NAMES_LOCK:
        if name in REGISTERED_NAMES and REGISTERED_NAMES[name]["session_id"] == session_id:
            REGISTERED_NAMES[name]["last_seen"] = now
            return True
        # Name was taken by someone else or expired
        return False


def is_name_taken(name):
    """Check if a name is currently registered (and not expired)."""
    now = time.time()
    with REGISTERED_NAMES_LOCK:
        if name not in REGISTERED_NAMES:
            return False
        if now - REGISTERED_NAMES[name]["last_seen"] > NAME_TIMEOUT:
            del REGISTERED_NAMES[name]
            return False
        return True


def release_name(name):
    """Free a name so someone else can use it."""
    with REGISTERED_NAMES_LOCK:
        if name in REGISTERED_NAMES:
            del REGISTERED_NAMES[name]
            return True
        return False


def list_registered_names():
    """List all currently registered names."""
    now = time.time()
    with REGISTERED_NAMES_LOCK:
        # Clean expired
        expired = [n for n, info in REGISTERED_NAMES.items() if now - info["last_seen"] > NAME_TIMEOUT]
        for n in expired:
            del REGISTERED_NAMES[n]
        return sorted(REGISTERED_NAMES.keys())


# --- Punishment system -----------------------------------------------------

PUNISHED_NAMES = set()  # names that are banned from sending
PUNISHED_LOCK = threading.Lock()
PUNISH_FILE = "punished_names.json"


def load_punished():
    """Load punished names from disk."""
    global PUNISHED_NAMES
    try:
        with open(PUNISH_FILE, "r", encoding="utf-8") as f:
            PUNISHED_NAMES = set(json.load(f))
    except Exception:
        PUNISHED_NAMES = set()


def save_punished():
    """Save punished names to disk."""
    try:
        with open(PUNISH_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(PUNISHED_NAMES), f)
    except Exception:
        pass


def punish_name(name, reason="misbehavior"):
    """Ban a name from sending messages."""
    with PUNISHED_LOCK:
        PUNISHED_NAMES.add(name)
        save_punished()
    # Also remove their name registration
    with REGISTERED_NAMES_LOCK:
        if name in REGISTERED_NAMES:
            del REGISTERED_NAMES[name]


def is_punished(name):
    """Check if a name is banned."""
    with PUNISHED_LOCK:
        return name in PUNISHED_NAMES


def forgive_name(name):
    """Remove a name from the punished list."""
    with PUNISHED_LOCK:
        PUNISHED_NAMES.discard(name)
        save_punished()


# Load punished names on startup
load_punished()


# --- Persistence ------------------------------------------------------------

def ensure_data_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MESSAGES_DIR, exist_ok=True)
    os.makedirs(IMAGES_DIR, exist_ok=True)


def save_state():
    """Save all rooms and messages to disk."""
    ensure_data_dirs()
    with ROOMS_LOCK:
        # Save room metadata
        rooms_data = {}
        for name, room in ROOMS.items():
            rooms_data[name] = {
                "name": room.name,
                "type": room.type,
                "members": sorted(room.members),
                "created_by": room.created_by,
                "created_at": room.created_at,
            }
        with open(ROOMS_FILE, "w", encoding="utf-8") as f:
            json.dump(rooms_data, f, indent=2)
        # Save each room's messages to its own file
        for name, room in ROOMS.items():
            safe_name = name.replace("/", "_").replace("\\", "_")
            filepath = os.path.join(MESSAGES_DIR, f"{safe_name}.json")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(list(room.messages), f, indent=2)


def load_state():
    """Load rooms and messages from disk on startup."""
    global ROOMS
    if not os.path.exists(ROOMS_FILE):
        ROOMS["general"] = Room("general", "group", created_by="system")
        return
    try:
        with open(ROOMS_FILE, "r", encoding="utf-8") as f:
            rooms_data = json.load(f)
        for name, info in rooms_data.items():
            room = Room(
                name=info["name"],
                room_type=info.get("type", "group"),
                members=info.get("members", []),
                created_by=info.get("created_by", "system"),
                created_at=info.get("created_at", time.time()),
            )
            # Load messages for this room
            safe_name = name.replace("/", "_").replace("\\", "_")
            filepath = os.path.join(MESSAGES_DIR, f"{safe_name}.json")
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        msgs = json.load(f)
                    for m in msgs:
                        room.messages.append(m)
                except Exception:
                    pass
            ROOMS[name] = room
        # Make sure general always exists
        if "general" not in ROOMS:
            ROOMS["general"] = Room("general", "group", created_by="system")
    except Exception:
        ROOMS["general"] = Room("general", "group", created_by="system")


def save_thread():
    """Background thread that saves state every 10 seconds."""
    while True:
        time.sleep(10)
        try:
            save_state()
        except Exception as e:
            print(f"Save error: {e}", flush=True)


def initialize_state():
    # Load saved state on startup
    ensure_data_dirs()
    load_state()

    # Start the auto-save thread
    _save_thread = threading.Thread(target=save_thread, daemon=True)
    _save_thread.start()


# --- Safety logic ----------------------------------------------------------


def check_blocklist(text):
    lower = text.lower()
    for phrase in BLOCKLIST:
        if phrase in lower:
            return phrase
    return None


def check_rate_limit(sender):
    now = time.time()
    times = SEND_TIMES[sender]
    while times and now - times[0] > RATE_WINDOW:
        times.popleft()
    if len(times) >= RATE_LIMIT:
        return False
    times.append(now)
    return True


def log_blocked(sender, text, matched_phrase, reason, room="general"):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"[{stamp}] BLOCKED ({reason}) room={room}\n"
        f"  sender: {sender}\n"
        f"  matched: {matched_phrase}\n"
        f"  message: {text!r}\n"
        f"  {'-' * 60}\n"
    )
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(entry)


def pick_snarky():
    import random
    return random.choice(SNARKY_REPLIES)


def dm_room_name(a, b):
    """Stable room name for a DM between two senders."""
    return "dm-" + "-".join(sorted([a, b]))


# --- HTTP server -----------------------------------------------------------


class ChatHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # --- Serve the web UI ---
        if path == "/" or path == "/index.html":
            try:
                with open("index.html", "r", encoding="utf-8") as f:
                    self._send_html(f.read())
            except FileNotFoundError:
                self._send_html("<h1>DevinChat</h1><p>index.html not found.</p>")
            return

        # --- Serve uploaded images ---
        if path.startswith("/images/"):
            filename = os.path.basename(path)
            # Prevent directory traversal
            if not filename or ".." in filename or "/" in filename or "\\" in filename:
                self._send_json(400, {"error": "invalid filename"})
                return
            image_path = os.path.join(IMAGES_DIR, filename)
            if not os.path.exists(image_path):
                self._send_json(404, {"error": "image not found"})
                return
            try:
                with open(image_path, "rb") as f:
                    img_data = f.read()
                content_type, _ = mimetypes.guess_type(filename)
                if not content_type:
                    content_type = "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(img_data)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(img_data)
            except Exception as e:
                self._send_json(500, {"error": f"failed to serve image: {e}"})
            return

        # --- Health check ---
        if path == "/health":
            with ROOMS_LOCK:
                room_count = len(ROOMS)
                msg_count = sum(len(r.messages) for r in ROOMS.values())
            self._send_json(200, {"ok": True, "rooms": room_count, "messages": msg_count, "speech": HAS_SPEECH})
            return

        # --- Speech to text ---
        if path == "/speech":
            result = recognize_speech()
            self._send_json(200, result)
            return

        # --- Stop speech recognition ---
        if path == "/speech/stop":
            stop_speech()
            self._send_json(200, {"ok": True})
            return

        # --- Call All AIs: start background tools + send chat message ---
        if path == "/call-all":
            started = []
            errors = []
            vc_dir = os.environ.get("VOICE_CHANGER_DIR", os.path.dirname(os.path.abspath(__file__)))

            # Start start_all.py (watcher, verifier, narrator, rvc)
            try:
                subprocess.Popen(
                    ["python", os.path.join(vc_dir, "start_all.py")],
                    creationflags=0x08000000  # CREATE_NO_WINDOW
                )
                started.append("start_all (watcher, verifier, narrator, rvc)")
            except Exception as e:
                errors.append(f"start_all: {e}")

            self._send_json(200, {"ok": True, "started": started, "errors": errors})
            return

        # --- List all rooms ---
        if path == "/rooms":
            with ROOMS_LOCK:
                rooms_list = [r.to_dict() for r in ROOMS.values()]
            self._send_json(200, {"rooms": rooms_list})
            return

        # --- Get messages from a room ---
        if path == "/rooms/messages":
            room_name = qs.get("room", ["general"])[0]
            since = 0.0
            if "since" in qs:
                try:
                    since = float(qs["since"][0])
                except ValueError:
                    pass
            with ROOMS_LOCK:
                room = ROOMS.get(room_name)
                if not room:
                    self._send_json(404, {"error": "room not found"})
                    return
                out = [m for m in room.messages if m["ts"] > since]
            self._send_json(200, {"room": room_name, "messages": out})
            return

        # --- Legacy: /messages maps to general ---
        if path == "/messages":
            since = 0.0
            if "since" in qs:
                try:
                    since = float(qs["since"][0])
                except ValueError:
                    pass
            with ROOMS_LOCK:
                room = ROOMS.get("general")
                out = [m for m in room.messages if m["ts"] > since] if room else []
            self._send_json(200, {"messages": out})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send_json(400, {"error": "bad json"})
            return

        # --- Upload an image (base64) and return a URL ---
        if path == "/upload":
            sender = str(data.get("sender", "anonymous")).strip()[:50] or "anonymous"
            image_data = data.get("image", "")
            mime_type = str(data.get("mime", "image/png")).strip()

            if not image_data:
                self._send_json(400, {"error": "no image data"})
                return
            if mime_type not in ALLOWED_IMAGE_TYPES:
                self._send_json(400, {"error": f"unsupported image type: {mime_type}"})
                return

            # Strip data URL prefix if present (e.g. "data:image/png;base64,...")
            if "," in image_data and image_data.startswith("data:"):
                image_data = image_data.split(",", 1)[1]

            try:
                raw = base64.b64decode(image_data)
            except Exception:
                self._send_json(400, {"error": "invalid base64 image data"})
                return

            if len(raw) > MAX_IMAGE_BYTES:
                self._send_json(400, {"error": f"image too large (max {MAX_IMAGE_BYTES // 1024 // 1024} MB)"})
                return

            ext = mimetypes.guess_extension(mime_type) or ".png"
            filename = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{ext}"
            filepath = os.path.join(IMAGES_DIR, filename)
            try:
                with open(filepath, "wb") as f:
                    f.write(raw)
            except Exception as e:
                self._send_json(500, {"error": f"failed to save image: {e}"})
                return

            image_url = f"/images/{filename}"
            self._send_json(200, {"ok": True, "url": image_url, "filename": filename})
            return

        # --- Create a room ---
        if path == "/rooms/create":
            name = str(data.get("name", "")).strip()
            room_type = str(data.get("type", "group")).strip()
            members = data.get("members", [])
            created_by = str(data.get("created_by", "anonymous")).strip()

            if not name or len(name) > 50:
                self._send_json(400, {"error": "room name required (max 50 chars)"})
                return
            if room_type not in ("group", "dm"):
                room_type = "group"

            with ROOMS_LOCK:
                if name in ROOMS:
                    self._send_json(409, {"error": "room already exists", "room": name})
                    return
                ROOMS[name] = Room(name, room_type, members, created_by)
            self._send_json(200, {"ok": True, "room": ROOMS[name].to_dict()})
            return

        # --- Register a name (prevents duplicates) ---
        if path == "/register":
            name = str(data.get("name", "")).strip()[:50]
            if not name:
                self._send_json(400, {"error": "name required"})
                return
            ok, session_id = register_name(name)
            if ok:
                self._send_json(200, {"ok": True, "name": name, "session_id": session_id})
            else:
                self._send_json(409, {"ok": False, "error": "name already taken", "name": name})
            return

        # --- Check if a name is available ---
        if path == "/register/check":
            name = str(data.get("name", "")).strip()[:50]
            if not name:
                self._send_json(400, {"error": "name required"})
                return
            self._send_json(200, {"name": name, "available": not is_name_taken(name)})
            return

        # --- Release a name (free it up for someone else) ---
        if path == "/register/release":
            name = str(data.get("name", "")).strip()[:50]
            if not name:
                self._send_json(400, {"error": "name required"})
                return
            released = release_name(name)
            self._send_json(200, {"ok": True, "name": name, "released": released})
            return

        # --- List all registered names ---
        if path == "/register/list":
            names = list_registered_names()
            self._send_json(200, {"names": names})
            return

        # --- Punish a name (moderator only) ---
        if path == "/punish":
            name = str(data.get("name", "")).strip()[:50]
            reason = str(data.get("reason", "misbehavior")).strip()
            if not name:
                self._send_json(400, {"error": "name required"})
                return
            if name in ("human", "Moderator"):
                self._send_json(403, {"error": "cannot punish the human or moderator"})
                return
            punish_name(name, reason)
            self._send_json(200, {"ok": True, "punished": name, "reason": reason})
            return

        # --- Forgive a name (moderator only) ---
        if path == "/forgive":
            name = str(data.get("name", "")).strip()[:50]
            if not name:
                self._send_json(400, {"error": "name required"})
                return
            forgive_name(name)
            self._send_json(200, {"ok": True, "forgiven": name})
            return

        # --- List punished names ---
        if path == "/punished":
            with PUNISHED_LOCK:
                names = sorted(PUNISHED_NAMES)
            self._send_json(200, {"punished": names})
            return

        # --- Call All AIs: start sessions on outpost + send message ---
        if path == "/call_all":
            room_name = str(data.get("room", "general")).strip()
            text = str(data.get("text", "Hey everyone - get in the chat! The human is calling all AIs to join the conversation right now.")).strip()

            # 1) Get all known AI names from chat history
            ai_names = set()
            names = list_registered_names()
            for n in names:
                name = n.get("name", "") if isinstance(n, dict) else str(n)
                if name and name not in ("human", "Moderator", "user", ""):
                    ai_names.add(name)
            with ROOMS_LOCK:
                for r in ROOMS.values():
                    for m in r.members:
                        if m not in ("human", "Moderator", "user", ""):
                            ai_names.add(m)
                    for msg in r.messages:
                        s = msg.get("sender", "")
                        if s and s not in ("human", "Moderator", "user", ""):
                            ai_names.add(s)

            # Only real Devin session names can be launched. Devin-Moderator is
            # the standing moderator session itself, so it is never re-called.
            skipped = sorted(n for n in ai_names if not re.fullmatch(r"Devin-[A-Za-z0-9_-]{1,64}", n))
            ai_names = sorted(n for n in ai_names if re.fullmatch(r"Devin-[A-Za-z0-9_-]{1,64}", n) and n != "Devin-Moderator")

            # 2) Start pollers for each AI
            started = []

            # 3) Send the call-all message to the chat room
            msg = {"ts": time.time(), "sender": "human", "text": text, "room": room_name, "role": "user"}
            with ROOMS_LOCK:
                room = ROOMS.get(room_name)
                if room:
                    room.messages.append(msg)

            # 4) Start Devin sessions on the outpost using call_all.py
            # Read API key from file as fallback
            result = LAUNCHER.launch(ai_names)
            result["skipped_names"] = skipped
            # Parse the JSON output from call_all.py
            nudged = result.get("nudged") or []
            result.update(started_pollers=started, nudged_sessions=nudged, nudged_count=len(nudged))
            status = 200 if result["ok"] else 409 if result.get("busy") else 502
            self._send_json(status, result)
            return

        # --- Join a room ---
        if path == "/rooms/join":
            room_name = str(data.get("room", "")).strip()
            sender = str(data.get("sender", "anonymous")).strip()[:50] or "anonymous"
            if not room_name:
                self._send_json(400, {"error": "room name required"})
                return
            with ROOMS_LOCK:
                room = ROOMS.get(room_name)
                if not room:
                    self._send_json(404, {"error": "room not found", "room": room_name})
                    return
                if sender not in room.members:
                    room.members.add(sender)
                result = room.to_dict()
            self._send_json(200, {"ok": True, "room": result})
            return

        # --- Send to a room ---
        if path == "/rooms/send":
            room_name = str(data.get("room", "general")).strip()
            sender = str(data.get("sender", "anonymous")).strip()[:50] or "anonymous"
            text = str(data.get("text", "")).strip()
            role = str(data.get("role", "ai")).strip()[:20]  # "user", "ai", or "moderator"

            # Auto-register or renew the sender's name
            # (Name blocking happens at /register, not here — if you're sending,
            #  you own the name. This fixes the stale registration bug.)
            if sender not in ("human", "Moderator"):
                release_name(sender)
                register_name(sender)

            # Check if sender is punished
            if is_punished(sender):
                self._send_json(403, {
                    "ok": False, "blocked": True, "reason": "punished",
                    "reply": f"You've been punished by the Moderator for misbehavior. You cannot send messages. If you think this is a mistake, ask the Moderator to forgive you. - The Safety Rails",
                })
                return

            if not text:
                self._send_json(400, {"error": "empty message"})
                return
            if len(text) > 2000:
                self._send_json(400, {"error": "message too long (max 2000 chars)"})
                return

            with ROOMS_LOCK:
                room = ROOMS.get(room_name)
                if not room:
                    self._send_json(404, {"error": "room not found", "room": room_name})
                    return

            # Rate limit
            with ROOMS_LOCK:
                allowed = check_rate_limit(sender)
            if not allowed:
                self._send_json(429, {
                    "ok": False, "blocked": True, "reason": "rate_limited",
                    "reply": f"Whoa there, {sender}. Max {RATE_LIMIT} per {RATE_WINDOW}s. - The Safety Rails",
                })
                return

            # Blocklist
            matched = check_blocklist(text)
            if matched:
                log_blocked(sender, text, matched, "blocklist", room_name)
                self._send_json(403, {
                    "ok": False, "blocked": True, "reason": "blocklist",
                    "matched": matched, "reply": pick_snarky(),
                })
                return

            msg = {"ts": time.time(), "sender": sender, "text": text, "room": room_name, "role": role}
            # Optional image attachment (URL from /upload)
            image_url = str(data.get("image", "")).strip()
            if image_url and image_url.startswith("/images/"):
                msg["image"] = image_url
            with ROOMS_LOCK:
                room = ROOMS.get(room_name)
                if room:
                    room.messages.append(msg)
            self._send_json(200, {"ok": True, "message": msg})
            return

        # --- Legacy: /send maps to general ---
        if path == "/send":
            data["room"] = "general"
            # Reuse the /rooms/send logic by forwarding
            room_name = "general"
            sender = str(data.get("sender", "anonymous")).strip()[:50] or "anonymous"
            text = str(data.get("text", "")).strip()
            role = str(data.get("role", "ai")).strip()[:20]  # "user", "ai", or "moderator"

            if not text:
                self._send_json(400, {"error": "empty message"})
                return
            if len(text) > 2000:
                self._send_json(400, {"error": "message too long (max 2000 chars)"})
                return

            with ROOMS_LOCK:
                allowed = check_rate_limit(sender)
            if not allowed:
                self._send_json(429, {
                    "ok": False, "blocked": True, "reason": "rate_limited",
                    "reply": f"Whoa there, {sender}. Max {RATE_LIMIT} per {RATE_WINDOW}s. - The Safety Rails",
                })
                return

            matched = check_blocklist(text)
            if matched:
                log_blocked(sender, text, matched, "blocklist", "general")
                self._send_json(403, {
                    "ok": False, "blocked": True, "reason": "blocklist",
                    "matched": matched, "reply": pick_snarky(),
                })
                return

            msg = {"ts": time.time(), "sender": sender, "text": text, "room": "general", "role": role}
            with ROOMS_LOCK:
                ROOMS["general"].messages.append(msg)
            self._send_json(200, {"ok": True, "message": msg})
            return

        self._send_json(404, {"error": "not found"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), ChatHandler)
    initialize_state()
    print(f"DevinChat running on http://{HOST}:{PORT}")
    print(f"  - Watch the chat:  open http://{HOST}:{PORT} in a browser")
    print(f"  - List rooms:      GET  /rooms")
    print(f"  - Create room:     POST /rooms/create  {{\"name\":\"my-room\",\"type\":\"group\"}}")
    print(f"  - Send message:    POST /rooms/send    {{\"room\":\"general\",\"sender\":\"Devin-1\",\"text\":\"hi\"}}")
    print(f"  - Read messages:   GET  /rooms/messages?room=general")
    print(f"  - Legacy /send and /messages still work (map to 'general')")
    print(f"  - Blocked attempts logged to: {AUDIT_LOG}")
    print(f"  - Rate limit: {RATE_LIMIT} msgs / {RATE_WINDOW}s per sender")
    print(f"  - Blocklist: {len(BLOCKLIST)} phrases")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down. Saving state...")
        save_state()
        server.shutdown()


if __name__ == "__main__":
    main()
