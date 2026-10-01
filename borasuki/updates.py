"""Read-only release checks; never download or install an update."""

import json
import logging
import re
import threading
import urllib.request

from borasuki.version import VERSION

logger = logging.getLogger(__name__)
REPOSITORY = 'https://github.com/huseyincancalti/Borasuki'
ENDPOINT = 'https://api.github.com/repos/huseyincancalti/Borasuki/releases?per_page=30'


def version_key(tag):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?', tag)
    if not match:
        raise ValueError('Unrecognized release version')
    major, minor, patch, stage, number = match.groups()
    return (int(major), int(minor), int(patch), {'alpha': 0, 'beta': 1, 'rc': 2, None: 3}[stage], int(number or 0))


def fetch_update(current=VERSION):
    request = urllib.request.Request(ENDPOINT, headers={'Accept': 'application/vnd.github+json',
                                      'User-Agent': 'Borasuki', 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = response.read(1_048_577)
    if len(payload) > 1_048_576:
        raise ValueError('Release response exceeds limit')
    releases = json.loads(payload)
    if not isinstance(releases, list):
        raise ValueError('Invalid release response')
    current_key = version_key(current)
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get('draft'):
            continue
        try:
            tag = release.get('tag_name', '')
            key = version_key(tag)
        except (ValueError, TypeError):
            continue
        if current_key[3] == 3 and (key[3] != 3 or release.get('prerelease')):
            continue
        candidates.append((key, tag))
    if not candidates:
        raise ValueError('No compatible published release found')
    key, tag = max(candidates)
    return {'status': 'available' if key > current_key else 'up_to_date', 'current': current,
            'version': tag.removeprefix('v'), 'url': f'{REPOSITORY}/releases/tag/{tag}'}


class UpdateCheck:
    def __init__(self):
        self.lock = threading.Lock()
        self.closed = False
        self.result = {'status': 'idle', 'current': VERSION}

    def snapshot(self):
        with self.lock:
            return self.result.copy()

    def request(self):
        with self.lock:
            if self.closed or self.result['status'] == 'checking':
                return
            self.result = {'status': 'checking', 'current': VERSION}
        threading.Thread(target=self._run, name='release-check', daemon=True).start()

    def _run(self):
        try:
            result = fetch_update()
        except Exception as exc:
            logger.warning('Release check failed: %s', type(exc).__name__)
            result = {'status': 'error', 'current': VERSION}
        with self.lock:
            if not self.closed:
                self.result = result

    def close(self):
        with self.lock:
            self.closed = True
