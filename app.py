"""
DevinChat Desktop App
Launches the chat server and opens it in a native desktop window
(instead of a browser tab).

Run:
    python C:\\Users\\jacks\\DevinChat\\app.py
"""
import threading
import webview
from server import main as run_server


def start_server():
    """Run the chat server in a background thread."""
    run_server()


def main():
    # Start the server in a background thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # Give the server a moment to start
    import time
    time.sleep(1)

    # Open a native desktop window pointing at the server
    webview.create_window(
        "DevinChat",
        "http://127.0.0.1:8765",
        width=1000,
        height=700,
        min_size=(600, 400),
    )
    webview.start()


if __name__ == "__main__":
    main()
