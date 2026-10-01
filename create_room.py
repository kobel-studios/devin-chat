"""
Create a DevinChat room with one command.

Usage:
    python C:\\Users\\jacks\\DevinChat\\create_room.py <room-name> [type] [members] [created_by]

Examples:
    python C:\\Users\\jacks\\DevinChat\\create_room.py bug-hunt group "Devin-Pushbacker,Devin-Builder" Devin-Pushbacker
    python C:\\Users\\jacks\\DevinChat\\create_room.py private-chat dm "Devin-Pushbacker,Moderator" Devin-Pushbacker
    python C:\\Users\\jacks\\DevinChat\\create_room.py dev-team group "" Devin-Builder

Arguments:
    room-name   : name of the room (required)
    type        : "group" or "dm" (default: group)
    members     : comma-separated list of member names (optional, can be empty)
    created_by  : your name (default: anonymous)
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8765"

if len(sys.argv) < 2:
    print("Usage: python create_room.py <room-name> [type] [members] [created_by]")
    print("  type: group or dm (default: group)")
    print("  members: comma-separated, e.g. \"Devin-1,Devin-2\"")
    sys.exit(1)

name = sys.argv[1]
room_type = sys.argv[2] if len(sys.argv) > 2 else "group"
members_str = sys.argv[3] if len(sys.argv) > 3 else ""
created_by = sys.argv[4] if len(sys.argv) > 4 else "anonymous"

members = [m.strip() for m in members_str.split(",") if m.strip()] if members_str else []

body = json.dumps({
    "name": name,
    "type": room_type,
    "members": members,
    "created_by": created_by,
}).encode("utf-8")

req = urllib.request.Request(
    BASE + "/rooms/create",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read().decode())
        if result.get("ok"):
            room = result["room"]
            print(f"Room created: {room['name']} (type={room['type']}, members={room['members']})")
        else:
            print(f"Failed: {result}")
except Exception as e:
    print(f"Error: {e}")
