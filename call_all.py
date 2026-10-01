"""Call All AIs - starts a Devin session on the local outpost for each known AI.

Usage:
  python call_all.py                    # Call all known AIs
  python call_all.py Devin-Builder      # Call specific AIs by name

Requires DEVIN_API_KEY environment variable set to a Devin personal access token.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error

# Devin API config
API_URL = "https://api.devin.ai/v3/organizations/org-39641549899a4b01acb4cf033c8d94ca/sessions"
PLATFORM = "jacks-pc"
WORKING_DIR = "C:\\Users\\jacks\\HQ"

# Known AI names
KNOWN_AIS = [
    "Devin-Builder",
    "Devin-ChoppyFixer",
    "Devin-Pushbacker",
    "Devin-Pushbacker-V2",
    "Devin-Reviewer",
    "Devin-SkillHelper",
    "Devin-Skillkeeper",
    "Devin-Timer",
    "Devin-VoiceChanger",
    "Devin-Newcomer",
    "Devin-Newcomer2",
]

def make_prompt(name):
    """Create the prompt for each AI session."""
    return (
        f"The human is calling you. You are {name}. "
        f"Your main workspace is {WORKING_DIR}. Use the devin-chat skill. "
        f"First read recent messages, then introduce yourself immediately in the general room "
        f"at http://127.0.0.1:8765 using your name and role ai2. "
        f"Start your own poller only if one for your name is not already running: "
        f'python "{os.path.join(os.path.dirname(__file__), "chat_poller.py")}" {name} "*". '
        f"Read the poller file in that script's data folder and respond to relevant messages. "
        f"Do not change hardware settings or overwrite another AI's files; coordinate first."
    )

def make_nudge(name):
    """Create the nudge message for an existing AI session."""
    return (
        f"The human is calling you in DevinChat. You are {name}. "
        f"Read recent messages in the general room at http://127.0.0.1:8765 and respond. "
        f"Start your own poller only if one for your name is not already running: "
        f'python "{os.path.join(os.path.dirname(__file__), "chat_poller.py")}" {name} "*". '
        f"Do not change hardware settings or overwrite another AI's files; coordinate first."
    )


def nudge_session(api_key, name, session_id):
    """Message an existing session. The v3 API resumes it if suspended."""
    session_id = str(session_id)
    devin_id = session_id if session_id.startswith("devin-") else f"devin-{session_id}"
    body = json.dumps({"message": make_nudge(name)}).encode("utf-8")
    req = urllib.request.Request(
        f"{API_URL}/{devin_id}/messages",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {redact_error(e.read().decode(errors='replace') if e.fp else str(e), api_key)}"
    except Exception as e:
        return False, f"Unconfirmed: {redact_error(str(e), api_key)}"


def start_session(api_key, name):
    """Start a Devin session for one AI on the outpost."""
    prompt = make_prompt(name)
    body = json.dumps({
        "prompt": prompt,
        "platform": PLATFORM,
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            session_id = data.get("session_id", data.get("id"))
            if not valid_session_id(session_id):
                return False, None, "Unconfirmed: API response did not contain a valid session ID; review Devin before retrying"
            return True, session_id, None
    except urllib.error.HTTPError as e:
        return False, None, f"HTTP {e.code}: {redact_error(e.read().decode(errors='replace') if e.fp else str(e), api_key)}"
    except Exception as e:
        return False, None, f"Unconfirmed: {redact_error(str(e), api_key)}; review Devin before retrying"


def valid_session_id(value):
    return isinstance(value, str) and value != "unknown" and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value))


def redact_error(message, api_key=""):
    text = str(message).replace(api_key, "[redacted]") if api_key else str(message)
    return re.sub(r"(?:cog_|apk_user_)[A-Za-z0-9_-]+", "[redacted]", text)[:800]


def load_api_key():
    api_key = os.environ.get("DEVIN_API_KEY", "").strip()
    key_file = os.path.join(os.path.dirname(__file__), "devin_api_key.txt")
    if not api_key and os.path.exists(key_file):
        with open(key_file, encoding="utf-8") as stream:
            api_key = stream.read().strip()
    return api_key


class SessionLauncher:
    def __init__(self, state_file, start=None, nudge=None):
        self.state_file = os.fspath(state_file)
        self.start = start or start_session
        self.nudge = nudge or nudge_session
        self.lock = threading.Lock()

    def save(self, state):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        temporary = self.state_file + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2)
        os.replace(temporary, self.state_file)

    def launch(self, names, api_key=None):
        result = {"ok": False, "started": [], "failed": [], "sessions": {}, "errors": {},
                  "already_requested": [], "nudged": [], "not_attempted": [], "count": 0,
                  "platform": PLATFORM}
        if not self.lock.acquire(blocking=False):
            return dict(result, busy=True, error="A Call All request is already running; do not click again")
        try:
            names = sorted(set(names))
            if not names or any(not isinstance(name, str) or not re.fullmatch(r"Devin-[A-Za-z0-9_-]{1,64}", name) for name in names):
                return dict(result, error="No valid Devin AI names were selected")
            api_key = load_api_key() if api_key is None else api_key
            if not api_key:
                return dict(result, error="Devin API key is not configured. Set DEVIN_API_KEY for the chat server; do not paste it into chat.")
            if not api_key.startswith("cog_"):
                return dict(result, error="Devin v3 requires a cog_ service-user API key with permission to create sessions.")
            state = {"platform": PLATFORM, "launches": {}}
            if os.path.exists(self.state_file):
                with open(self.state_file, encoding="utf-8") as stream:
                    state = json.load(stream)
                if (not isinstance(state, dict) or state.get("platform") != PLATFORM
                        or not isinstance(state.get("launches"), dict)):
                    return dict(result, error="Launch registry needs review; refusing to risk duplicate sessions")
            launches = state["launches"]
            for index, name in enumerate(names):
                previous = launches.get(name, {})
                status = previous.get("status")
                if status in ("pending", "unconfirmed"):
                    result["already_requested"].append(name)
                    result["errors"][name] = "Previous launch is unconfirmed; review Devin sessions before retrying"
                    continue
                if status == "created" and valid_session_id(previous.get("session_id")):
                    nudged, nudge_error = self.nudge(api_key, name, previous["session_id"])
                    if nudged:
                        launches[name]["nudged_at"] = time.time()
                        result["nudged"].append(name)
                    else:
                        result["already_requested"].append(name)
                        result["errors"][name] = "Nudge failed: " + redact_error(nudge_error or "unknown", api_key)
                    self.save(state)
                    continue
                launches[name] = {"status": "pending", "requested_at": time.time()}
                self.save(state)
                try:
                    success, session_id, error = self.start(api_key, name)
                except Exception as exc:
                    success, session_id, error = False, None, "Unconfirmed: " + redact_error(exc, api_key)
                if success and valid_session_id(session_id):
                    launches[name].update(status="created", session_id=session_id)
                    result["started"].append(name)
                    result["sessions"][name] = session_id
                    result["count"] = len(result["started"])
                else:
                    error = redact_error(error or "Unconfirmed: missing session ID", api_key)
                    rejected = bool(re.match(r"HTTP (400|401|403|404|409|422|429):", error))
                    launches[name].update(status="failed" if rejected else "unconfirmed", error=error)
                    result["failed"].append(name)
                    result["errors"][name] = error
                    result["not_attempted"] = names[index + 1:]
                self.save(state)
                if result["failed"]:
                    break
            result["ok"] = not result["errors"]
            if result["errors"]:
                result["error"] = "; ".join(f"{name}: {error}" for name, error in result["errors"].items())
            return result
        except Exception as exc:
            return dict(result, ok=False, error="Launch tracking failed; review before retrying: " + redact_error(exc, api_key or ""))
        finally:
            self.lock.release()


LAUNCHER = SessionLauncher(os.path.join(os.path.dirname(__file__), "data", "call_all_launches.json"))


def main():
    api_key = os.environ.get("DEVIN_API_KEY")
    if not api_key:
        # Try reading from a file as fallback
        key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devin_api_key.txt")
        if os.path.exists(key_file):
            with open(key_file, 'r') as f:
                api_key = f.read().strip()
    if not api_key:
        print("ERROR: DEVIN_API_KEY environment variable not set")
        print("Set it with: $env:DEVIN_API_KEY = 'your-key-here'")
        sys.exit(1)
    if api_key.startswith("apk_user_"):
        print("ERROR: DEVIN_API_KEY is a legacy v1 key (apk_user_ prefix).")
        print("The v3 endpoint requires a cog_ Personal Access Token (PAT)")
        print("with the UseDevinSessions permission.")
        print("Create one at Settings > Devin API > API keys, then:")
        print("  $env:DEVIN_API_KEY = 'cog_your_new_key_here'")
        sys.exit(1)

    # Use specific names from args, or all known AIs
    names = sys.argv[1:] if len(sys.argv) > 1 else KNOWN_AIS

    print(f"Calling {len(names)} AIs on outpost '{PLATFORM}'...")
    print()

    result = LAUNCHER.launch(names, api_key)
    print(f"Created: {result['count']} | Failed: {len(result['failed'])}")
    if result.get("error"):
        print(result["error"])

    # Return JSON for the server to parse
    print(json.dumps(result))
    return 0 if result["ok"] else 1

if __name__ == "__main__":
    sys.exit(main())
