"""Conservative ownership checks for application-generated temporary files."""

import json
import os
import re
from pathlib import Path


def safe_files(root, protected=(), *, recursive=True):
    root = Path(root)
    def check(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('error.unsafe_cleanup')
    for path in (root, *root.parents):
        check(path)
    resolved = root.resolve()
    if any(Path(path).resolve().is_relative_to(resolved) for path in protected):
        raise ValueError('error.unsafe_cleanup')
    if not recursive or not root.exists():
        return []
    if root.is_file():
        return [root]
    files = []
    def fail(error):
        raise error
    for parent, directories, names in os.walk(root, followlinks=False, onerror=fail):
        for name in directories + names:
            check(Path(parent) / name)
        files.extend(Path(parent) / name for name in names)
    return files


def orphan_job(root, protected):
    """Old render config is evidence, not permission to delete arbitrary trees."""
    files = safe_files(root, protected)
    configs = []
    for path in files:
        relative = path.relative_to(root)
        if len(relative.parts) > 2 or (len(relative.parts) == 2 and not re.fullmatch(r'revision-\d+', relative.parts[0])):
            raise ValueError('error.unsafe_cleanup')
        if not re.fullmatch(r'(config\.json(?:[^/]*\.tmp)?|source_info\.json|analysis\.json|source\.ffindex|source\.timecodes\.txt|render\.log|preview\.log|preview-(?:original|enhanced)\.(?:mkv|mp4)|model-build\.log|segments\.ffconcat|segment-\d{10}-\d{10}(?:\.part)?\.mkv)', path.name):
            raise ValueError('error.unsafe_cleanup')
        if path.name == 'config.json':
            if path.stat().st_size > 256 * 1024:
                raise ValueError('error.unsafe_cleanup')
            try:
                config = json.loads(path.read_text(encoding='utf-8'))
                if (config['id'] != root.name or Path(config['work']) != path.parent or
                        any(not isinstance(config[key], str) or not Path(config[key]).is_absolute() for key in ('source', 'output'))):
                    raise ValueError('error.unsafe_cleanup')
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError('error.unsafe_cleanup') from exc
            configs.append(config)
    if not configs:
        raise ValueError('error.unsafe_cleanup')
    safe_files(root, [*protected, *(config[key] for config in configs for key in ('source', 'output'))])
    return configs, files
