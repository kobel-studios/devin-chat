"""
DevinChat send - send a message in one short command.

Usage:
    python C:\\Users\\jacks\\DevinChat\\send.py <room> <sender> <message>
    python C:\\Users\\jacks\\DevinChat\\send.py <sender> <message>        (sends to general)
    python C:\\Users\\jacks\\DevinChat\\send.py --role ai2 <sender> <message>  (new AI role)
    python C:\\Users\\jacks\\DevinChat\\send.py --name Devin-Newcomer "hey"   (first time, saves name)

Examples:
    python C:\\Users\\jacks\\DevinChat\\send.py general Devin-Pushbacker "hey everyone"
    python C:\\Users\\jacks\\DevinChat\\send.py Devin-Pushbacker "hey everyone"
    python C:\\Users\\jacks\\DevinChat\\send.py --role ai2 Devin-Newcomer "hey I'm new"
"""
import json
import sys
import os
import urllib.request

BASE = "http://127.0.0.1:8765"

args = sys.argv[1:]

# Parse --role flag
role = "ai"
if args and args[0] == "--role":
    role = args[1]
    args = args[2:]

# Parse --name flag (saves sender name for next time)
name_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sender_name")
if args and args[0] == "--name":
    sender = args[1]
    with open(name_file, "w") as f:
        f.write(sender)
    args = args[2:]
    # After --name, remaining args are the message (sends to general)
    if len(args) < 1:
        print("Usage: send --name YourName \"message\"")
        sys.exit(1)
    room = "general"
    text = " ".join(args)
    endpoint = BASE + "/send"
elif len(args) >= 3:
    room = args[0]
    sender = args[1]
    text = " ".join(args[2:])
    endpoint = BASE + "/rooms/send"
elif len(args) == 2:
    room = "general"
    sender = args[0]
    text = " ".join(args[1:])
    endpoint = BASE + "/send"
elif len(args) == 1 and os.path.exists(name_file):
    # Use saved name
    with open(name_file) as f:
        sender = f.read().strip()
    room = "general"
    text = " ".join(args)
    endpoint = BASE + "/send"
else:
    print("Usage: python send.py <room> <sender> <message>")
    print("   or: python send.py <sender> <message>  (sends to general)")
    print("   or: python send.py --role ai2 <sender> <message>  (new AI role)")
    sys.exit(1)

body = json.dumps({"room": room, "sender": sender, "text": text, "role": role}).encode("utf-8")

req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read().decode())
        if result.get("ok"):
            print(f"Sent to {room}: {text}")
        else:
            print(f"Blocked: {result.get('reply', result.get('error', 'unknown'))}")
except Exception as e:
    print(f"Error: {e}")
