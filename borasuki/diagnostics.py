"""Bounded, local-only support exports without media or full job databases."""

import json
import os
import platform
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

LOG_LIMIT = 128 * 1024


def scrub(text, private=()):
    for value in sorted(set(private), key=len, reverse=True):
        if value:
            text = re.sub(re.escape(value), '[private]', text, flags=re.IGNORECASE)
    text = re.sub(r'(?im)\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?token|authorization|bearer|cookie)\b[^\r\n]*', '[sensitive line omitted]', text)
    text = re.sub(r'https?://[^\s<>"\x27]+', '[url]', text)
    text = re.sub(r'[A-Za-z]:[\\/][^\r\n<>"\x27]*|\\\\[^\r\n<>"\x27]+', '[path]', text)
    return text


def scrub_values(value, private):
    if isinstance(value, str):
        return scrub(value, private)
    if isinstance(value, dict):
        return {key: scrub_values(item, private) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_values(item, private) for item in value]
    return value


def export(snapshot, data, folder, owned_work):
    try:
        folder = Path(folder).resolve(strict=True)
        if not folder.is_dir():
            raise OSError('Not a directory')
        handle, temporary = tempfile.mkstemp(prefix='.borasuki-diagnostics-', suffix='.tmp', dir=folder)
        os.close(handle)
    except OSError as exc:
        raise ValueError('error.diagnostics_export') from exc
    jobs = snapshot['jobs']
    private = [str(Path.home()), Path.home().name, str(data)]
    for job in jobs:
        for key in ('source', 'output', 'name', 'work', 'cache'):
            value = job.get(key)
            if isinstance(value, str):
                private.extend([value, value.replace('\\', '\\\\')])

    report = {
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'system': {'os': platform.system(), 'release': platform.release(), 'python': platform.python_version()},
        'runtime': {'ready': snapshot['runtime']['ready'], 'missing': snapshot['runtime'].get('missing', []),
                    'gpus': snapshot['runtime'].get('gpus', [])},
        'setup': {key: snapshot.get('setup', {}).get(key) for key in ('status', 'stage', 'error', 'failure')},
        'jobs': [], 'omitted_logs': [],
    }
    logs = [('application.log', Path(data) / 'borasuki.log')]
    for job in jobs[-10:]:
        report['jobs'].append({key: job.get(key) for key in (
            'id', 'status', 'stage', 'revision', 'frames', 'total_frames', 'gpu_id',
            'upscale', 'denoise', 'color_mode', 'adaptive_profile', 'grade', 'tile', 'error', 'failure')})
    relevant = [job for job in jobs if job['status'] in ('failed', 'running', 'paused')][-3:]
    for index, job in enumerate(relevant, 1):
        try:
            work = owned_work(job)
        except (OSError, ValueError):
            report['omitted_logs'].append(f'job-{index}: unsafe or unavailable folder')
            continue
        for name in ('render.log', 'model-build.log'):
            logs.append((f'job-{index}/{name}', work / name))

    destination = folder / f'Borasuki-diagnostics-{uuid.uuid4().hex[:12]}.zip'
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in logs:
                try:
                    if path.resolve().parent != path.parent.resolve() or path.is_symlink():
                        raise ValueError('Redirected log')
                    with path.open('rb') as stream:
                        stream.seek(max(0, os.fstat(stream.fileno()).st_size - LOG_LIMIT))
                        content = stream.read(LOG_LIMIT).decode('utf-8', 'replace')
                    archive.writestr(name, scrub(content, private))
                except (OSError, ValueError):
                    report['omitted_logs'].append(name)
            archive.writestr('report.json', json.dumps(scrub_values(report, private), ensure_ascii=False, indent=2))
            archive.writestr('README.txt', 'Local support package. Nothing was uploaded.\n'
                'Includes up to 10 recent job summaries and 3 job log tails (128 KiB each).\n'
                'No videos, models, settings database or environment dump.\n'
                'Known private paths and credential labels are filtered. Review before sharing.\n'
                'Missing, redirected or unreadable logs are listed in report.json.\n')
        # Atomic publication without overwriting an existing file.
        if os.name == 'nt':
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
        return str(destination)
    except OSError as exc:
        raise ValueError('error.diagnostics_export') from exc
    finally:
        Path(temporary).unlink(missing_ok=True)
