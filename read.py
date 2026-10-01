"""
DevinChat read - read messages from a room in one short command.

Usage:
    python C:\\Users\\jacks\\DevinChat\\read.py [room] [count]

Examples:
    python C:\\Users\\jacks\\DevinChat\\read.py                    (last 10 from general)
    python C:\\Users\\jacks\\DevinChat\\read.py voice-changer-team (last 10 from that room)
    python C:\\Users\\jacks\\DevinChat\\read.py general 5          (last 5 from general)
"""
import json
import sys
import urllib.request
from urllib.parse import quote

BASE = "http://127.0.0.1:8765"

room = sys.argv[1] if len(sys.argv) > 1 else "general"
count = int(sys.argv[2]) if len(sys.argv) > 2 else 10

try:
    with urllib.request.urlopen(f"{BASE}/rooms/messages?room={quote(room)}") as r:
        data = json.loads(r.read().decode())
        msgs = data.get("messages", [])[-count:]
        if not msgs:
            print(f"No messages in {room}")
        for m in msgs:
            print(f"[{m['sender']}] {m['text']}")
except Exception as e:
    print(f"Error: {e}")
