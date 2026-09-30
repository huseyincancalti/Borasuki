"""Optional per-user Explorer and notification integration."""

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import winreg
from multiprocessing.connection import Client, Listener
from pathlib import Path

from borasuki.i18n import load_catalog, translate
from borasuki.storage import DATA, ROOT, atomic_json
from borasuki.batch import VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)
EXTENSIONS = VIDEO_EXTENSIONS
PIPE = r"\\.\pipe\Borasuki-" + hashlib.sha256(str(DATA).encode()).hexdigest()[:16]


def context_menu(enabled: bool, language: str):
    label = translate(load_catalog(language), "menu.upscale")
    command = subprocess.list2cmdline([str(Path(sys.executable).with_name("pythonw.exe")), str(ROOT / "main.py")]) + ' "%1"'
    for extension in EXTENSIONS:
        path = rf"Software\Classes\SystemFileAssociations\{extension}\shell\Borasuki"
        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, label)
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, str(ROOT / "frontend/assets/img/logo.ico"))
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path + r"\command") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
        else:
            for target in (path + r"\command", path):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, target)
                except FileNotFoundError:
                    pass


def notify(filename: str, language: str, data: Path):
    catalog = load_catalog(language)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId\Borasuki") as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Borasuki")
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(ROOT / "frontend/assets/img/logo.png"))
    payload = data / "notification.json"
    atomic_json(payload, {"title": translate(catalog, "notification.completed.title"),
                          "body": translate(catalog, "notification.completed.body", filename=filename)})
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                             str(ROOT / "borasuki/notify.ps1"), "-Payload", str(payload)],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace"))


def send_to_existing(source: str | None) -> None:
    secret = bytes.fromhex(json.loads((DATA / "ipc.json").read_text())["key"])
    with Client(PIPE, family="AF_PIPE", authkey=secret) as connection:
        connection.send_bytes(json.dumps({"source": source}).encode("utf-8"))


def start_listener(callback) -> Listener:
    secret = os.urandom(32)
    listener = Listener(PIPE, family="AF_PIPE", authkey=secret)
    atomic_json(DATA / "ipc.json", {"key": secret.hex()})

    def listen():
        while True:
            try:
                with listener.accept() as connection:
                    message = json.loads(connection.recv_bytes(32768))
                callback(message.get("source"))
            except (OSError, EOFError):
                return
            except Exception:
                logger.exception("Explorer import failed")
    threading.Thread(target=listen, name="explorer-import", daemon=True).start()
    return listener
