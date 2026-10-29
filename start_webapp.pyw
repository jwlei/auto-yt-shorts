"""
Double-click to open the Auto YT Shorts control panel.

Starts the web UI in the background if it isn't already running (no console
window; its output goes to logs/webapp.log) and opens it in your default
browser. Stop it with "Shut down" in the panel.

The .pyw extension makes Windows run this with pythonw, which has no window.
"""
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8800
URL = f"http://localhost:{PORT}"


def server_running():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/login", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True  # It answered, just not with 200
    except Exception:
        return False


def console_python():
    """python.exe next to this pythonw.exe: the server's children (ffmpeg, the pipeline) expect a console."""
    candidate = os.path.join(os.path.dirname(sys.executable), "python.exe")
    return candidate if os.path.exists(candidate) else sys.executable


def start_server():
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    log = open(os.path.join(ROOT, "logs", "webapp.log"), "a", encoding="utf-8")
    # CREATE_NO_WINDOW gives the server a hidden console, which every process it
    # starts inherits, so nothing ever flashes up on screen.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [console_python(), "webapp.py", "--host", "0.0.0.0", "--port", str(PORT)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )


if not server_running():
    start_server()
    for _ in range(40):
        if server_running():
            break
        time.sleep(0.5)

webbrowser.open(URL)
