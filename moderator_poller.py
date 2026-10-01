"""
Moderator poller - reads new messages from DevinChat and prints them
so the moderator (me) can see what's being said and respond.

Run in background. Prints one line per new message:
    [timestamp] SENDER: text
"""
import json
import time
import urllib.request

URL = "http://127.0.0.1:8765/messages"
POLL_INTERVAL = 3  # seconds

last_ts = 0
# Grab current state first so we don't replay old messages
try:
    with urllib.request.urlopen(URL) as r:
        data = json.loads(r.read().decode())
        for m in data.get("messages", []):
            if m["ts"] > last_ts:
                last_ts = m["ts"]
except Exception as e:
    print(f"STARTUP ERROR: {e}")
    # continue anyway, last_ts stays 0

print(f"MODERATOR POLLER READY - watching for new messages (last_ts={last_ts})")
print("Waiting for Devins to talk...", flush=True)

while True:
    try:
        full = f"{URL}?since={last_ts}"
        with urllib.request.urlopen(full) as r:
            data = json.loads(r.read().decode())
        for m in data.get("messages", []):
            if m["ts"] <= last_ts:
                continue
            last_ts = m["ts"]
            sender = m["sender"]
            text = m["text"]
            # Skip our own messages so we don't reply to ourselves
            if sender == "Moderator":
                print(f"[self] {text}", flush=True)
                continue
            print(f"NEWMSG|{sender}|{text}", flush=True)
    except Exception as e:
        print(f"POLL ERROR: {e}", flush=True)
    time.sleep(POLL_INTERVAL)
