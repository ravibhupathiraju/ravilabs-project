"""Unattended launcher for the Swing Trade Tracker.

Use THIS (not `python webapp.py`) for headless / auto-start running:
`webapp.py`'s dev entrypoint only brings the monitor up via the Werkzeug
reloader child, which is fragile for an all-day background process. Here we
start the live monitor directly, then serve on localhost with the reloader
off -- one clean process the Task Scheduler can wake and run.

Binds to 127.0.0.1 only, so the web UI is never exposed to the network; the
monitor still reaches Alpaca / market-data APIs outbound as normal.

    pythonw serve.py        # no console window (for the scheduled task)
    python  serve.py        # with console, to watch the logs
"""

import socket
import sys

from webapp import app, _autostart_monitors

HOST, PORT = "127.0.0.1", 5000


def _already_running(host: str, port: int) -> bool:
    """True if something is already listening on host:port -- i.e. another
    instance is up. Guards against two monitors trading the same accounts."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


if __name__ == "__main__":
    if _already_running(HOST, PORT):
        print(f"[serve] {HOST}:{PORT} already in use -- another instance is "
              "running; exiting so we never start a second monitor.")
        sys.exit(0)
    _autostart_monitors()   # 0DTE + swing monitors live immediately
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
