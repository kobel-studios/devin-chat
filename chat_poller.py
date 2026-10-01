"""
Devin chat poller - run this in the background to watch for new messages.
Writes new messages to a FILE so the AI can read them without shell confusion.

Usage:
    python C:\\Users\\jacks\\DevinChat\\chat_poller.py "YourName"

    # To watch a specific room instead of general:
    python C:\\Users\\jacks\\DevinChat\\chat_poller.py "YourName" "room-name"

    # To watch ALL rooms:
    python C:\\Users\\jacks\\DevinChat\\chat_poller.py "YourName" "*"

New messages are written to: C:\\Users\\jacks\\DevinChat\\data\\poller_<name>.txt
The AI should read that file to see new messages - NOT the terminal output.
Each line in the file looks like:
    NEWMSG|room-name|SenderName|their message
The file is cleared after each read by the AI (or auto-clears old messages).
"""
import json
import sys
import time
import urllib.request
import os

# Fix encoding for special characters (emojis, unicode, etc.)
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE = "http://127.0.0.1:8765"
POLL_INTERVAL = 5

my_name = sys.argv[1] if len(sys.argv) > 1 else "anonymous"
watch_room = sys.argv[2] if len(sys.argv) > 2 else "general"
watch_all = watch_room == "*"

# File where new messages are written for the AI to read
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MSG_FILE = os.path.join(DATA_DIR, f"poller_{my_name.replace(' ', '_')}.txt")

last_ts = {}  # room -> last timestamp seen
known_rooms = set()


def write_msgs(msgs):
    """Append new messages to the file. AI reads and clears this file."""
    if not msgs:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MSG_FILE, "a", encoding="utf-8") as f:
        for line in msgs:
            f.write(line + "\n")


def get_rooms():
    try:
        with urllib.request.urlopen(BASE + "/rooms", timeout=10) as r:
            data = json.loads(r.read().decode('utf-8'))
            return [rm["name"] for rm in data.get("rooms", [])]
    except Exception:
        return []


def init_room(room):
    """Grab current state so we don't replay old messages."""
    try:
        url = f"{BASE}/rooms/messages?room={room}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode('utf-8'))
            for m in data.get("messages", []):
                if m["ts"] > last_ts.get(room, 0):
                    last_ts[room] = m["ts"]
    except Exception:
        pass


# Initialize
if watch_all:
    for rm in get_rooms():
        known_rooms.add(rm)
        init_room(rm)
else:
    init_room(watch_room)

# Clear the message file on startup
os.makedirs(DATA_DIR, exist_ok=True)
with open(MSG_FILE, "w", encoding="utf-8") as f:
    pass

print(f"POLLER READY - watching as '{my_name}'", flush=True)
print(f"  room: {watch_room}", flush=True)
print(f"  msg_file: {MSG_FILE}", flush=True)
print(f"  To read new messages: read {MSG_FILE}", flush=True)
print("Waiting for new messages...", flush=True)

while True:
    new_msgs = []
    try:
        if watch_all:
            # Discover new rooms
            for rm in get_rooms():
                if rm not in known_rooms:
                    known_rooms.add(rm)
                    init_room(rm)
            rooms_to_check = list(known_rooms)
        else:
            rooms_to_check = [watch_room]

        for room in rooms_to_check:
            since = last_ts.get(room, 0)
            url = f"{BASE}/rooms/messages?room={room}&since={since}"
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    data = json.loads(r.read().decode('utf-8'))
            except Exception:
                continue
            for m in data.get("messages", []):
                if m["ts"] <= last_ts.get(room, 0):
                    continue
                last_ts[room] = m["ts"]
                sender = m["sender"]
                text = m["text"]
                if sender == my_name:
                    continue
                # Sanitize text to avoid encoding crashes
                text = text.encode('utf-8', errors='replace').decode('utf-8')
                if watch_all or len(rooms_to_check) > 1:
                    new_msgs.append(f"NEWMSG|{room}|{sender}|{text}")
                else:
                    new_msgs.append(f"NEWMSG|{sender}|{text}")
    except Exception:
        pass  # Don't print errors - they confuse the AI
    
    # Write new messages to file
    if new_msgs:
        write_msgs(new_msgs)
    
    time.sleep(POLL_INTERVAL)
