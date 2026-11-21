"""Small Windows tray controller for the local DeepSeek bridge."""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.request import urlopen

import pystray
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
BASE_URL = f"http://{HOST}:{PORT}"
SERVER = None


def make_icon():
    image = Image.new("RGB", (64, 64), (35, 35, 42))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill=(75, 130, 220))
    draw.text((21, 17), "D", fill="white")
    return image


def healthy():
    try:
        with urlopen(BASE_URL + "/healthz", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def start_server(icon=None, item=None):
    global SERVER
    if SERVER and SERVER.poll() is None:
        return
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    out = (logs / "server.out").open("a", encoding="utf-8")
    err = (logs / "server.err").open("a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    SERVER = subprocess.Popen(
        [sys.executable, "app.py"], cwd=ROOT,
        stdout=out, stderr=err, creationflags=flags,
    )


def stop_server(icon=None, item=None):
    global SERVER
    if SERVER and SERVER.poll() is None:
        SERVER.terminate()
        try:
            SERVER.wait(timeout=5)
        except subprocess.TimeoutExpired:
            SERVER.kill()
    SERVER = None


def quit_app(icon, item):
    stop_server()
    icon.stop()


def main():
    if not healthy():
        start_server()
    menu = pystray.Menu(
        pystray.MenuItem("Start server", start_server),
        pystray.MenuItem("Stop server", stop_server),
        pystray.MenuItem("Check API", lambda *_: webbrowser.open(BASE_URL + "/healthz")),
        pystray.MenuItem("Open Swagger API", lambda *_: webbrowser.open(BASE_URL + "/docs")),
        pystray.MenuItem("Open project folder", lambda *_: os.startfile(ROOT)),
        pystray.MenuItem("Exit", quit_app),
    )
    pystray.Icon("deepseek", make_icon(), "DeepSeek local bridge", menu).run()


if __name__ == "__main__":
    main()
