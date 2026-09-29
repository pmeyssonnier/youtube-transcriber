from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from app.constants import APP_NAME
from app.instance_lock import AlreadyRunningError, InstanceLock


HOST = "127.0.0.1"
PORT = 8765
BASE_URL = f"http://{HOST}:{PORT}"
PROJECT_ROOT = Path(__file__).resolve().parent


def application_is_running() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=1) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("app") == APP_NAME
    except (OSError, ValueError, urllib.error.URLError):
        return False


def port_is_used() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


def open_browser_when_ready() -> None:
    for _ in range(60):
        if application_is_running():
            webbrowser.open(BASE_URL)
            return
        time.sleep(0.25)


def main() -> int:
    if application_is_running():
        webbrowser.open(BASE_URL)
        print("L'application est déjà ouverte. La fenêtre existante a été affichée.")
        return 0

    lock = InstanceLock(PROJECT_ROOT / ".app.lock")
    try:
        lock.acquire()
    except AlreadyRunningError:
        if application_is_running():
            webbrowser.open(BASE_URL)
            print("L'application est déjà ouverte. La fenêtre existante a été affichée.")
            return 0
        print("Une autre instance de l'application démarre déjà. Réessayez dans quelques secondes.")
        return 1

    try:
        if port_is_used():
            print(f"Le port {PORT} est déjà utilisé par un autre programme.")
            return 1
        threading.Thread(target=open_browser_when_ready, daemon=True).start()
        uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
