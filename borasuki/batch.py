"""Bounded video selection; never recurse into unselected subfolders."""

import os
from pathlib import Path

from borasuki.errors import failure_info

VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v')
MAX_FILES = 200


def collect(paths, *, folders=True):
    if not isinstance(paths, (list, tuple)) or not paths or len(paths) > MAX_FILES or any(not isinstance(p, str) or not p for p in paths):
        raise ValueError('error.batch_selection')
    selected, failures = {}, []

    def add(path):
        try:
            path = path.resolve(strict=True)
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                raise ValueError('error.batch_format')
            selected.setdefault(str(path).casefold(), str(path))
        except (OSError, ValueError) as exc:
            failures.append({'path': str(path), 'failure': failure_info(exc, 'error.source_access')})
        if len(selected) + len(failures) > MAX_FILES:
            raise ValueError('error.batch_limit')

    for value in paths:
        path = Path(value)
        if folders and path.is_dir():
            try:
                with os.scandir(path) as entries:
                    count = 0
                    for entry in entries:
                        count += 1
                        if count > 10000:
                            raise ValueError('error.batch_limit')
                        if entry.is_file() and Path(entry.name).suffix.lower() in VIDEO_EXTENSIONS:
                            add(Path(entry.path))
            except OSError as exc:
                failures.append({'path': value, 'failure': failure_info(exc, 'error.source_access')})
        else:
            add(path)
    return {'paths': sorted(selected.values(), key=str.casefold), 'failures': failures}
