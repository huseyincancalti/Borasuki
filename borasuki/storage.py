"""Durable settings and job snapshots."""

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_directory() -> Path:
    override = os.environ.get("BORASUKI_DATA_DIR")
    if override:
        return Path(override).resolve()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return (base / "KaraKedi" / "Borasuki").resolve()


DATA = data_directory()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS presets (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, body TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM jobs ORDER BY rowid")]

    def save(self, job: dict) -> None:
        self.save_many([job])

    def save_many(self, jobs: list[dict]) -> None:
        rows = [(job["id"], json.dumps(job, ensure_ascii=False, allow_nan=False)) for job in jobs]
        with self.connect() as db:
            db.executemany("INSERT INTO jobs VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", rows)

    def delete_many(self, identifiers: list[str]) -> None:
        with self.connect() as db:
            db.executemany("DELETE FROM jobs WHERE id = ?", [(identifier,) for identifier in identifiers])

    def presets(self) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM presets ORDER BY name")]

    def save_preset(self, preset: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO presets VALUES (?, ?, ?)", (preset['id'], preset['name'].casefold(),
                       json.dumps(preset, ensure_ascii=False, allow_nan=False)))

    def delete_preset(self, identifier: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM presets WHERE id = ?", (identifier,))
