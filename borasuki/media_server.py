"""Read-only, revocable local media capability with bounded range reads."""

import logging
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger(__name__)


def identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def byte_range(header, size):
    if not header:
        return 0, size - 1
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError('Invalid range')
    first, last = match.groups()
    if not first:
        length = int(last)
        if length <= 0:
            raise ValueError('Invalid suffix')
        return max(0, size - length), size - 1
    start, end = int(first), min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise ValueError('Unsatisfiable range')
    return start, end


class MediaServer:
    def __init__(self):
        self.lock = threading.Lock()
        self.entry = None
        self.closed = threading.Event()
        self.slots = threading.BoundedSemaphore(4)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Capability URLs must not enter logs.

            def do_HEAD(self):
                self.serve(False)

            def do_GET(self):
                self.serve(True)

            def finish_empty(self, status, size=None):
                self.send_response(status)
                self.send_header('Content-Length', '0')
                if size is not None:
                    self.send_header('Content-Range', f'bytes */{size}')
                self.end_headers()

            def serve(self, body):
                self.connection.settimeout(3)
                if self.headers.get('Host') != owner.host:
                    self.finish_empty(403)
                    return
                with owner.lock:
                    entry = owner.entry
                if owner.closed.is_set() or not entry or self.path != entry['route']:
                    self.finish_empty(404)
                    return
                if not owner.slots.acquire(blocking=False):
                    self.finish_empty(503)
                    return
                try:
                    with entry['path'].open('rb') as stream:
                        if identity(os.fstat(stream.fileno())) != entry['identity']:
                            self.finish_empty(409)
                            return
                        size = entry['identity'][2]
                        try:
                            first, last = byte_range(self.headers.get('Range'), size)
                        except ValueError:
                            self.finish_empty(416, size)
                            return
                        self.send_response(206 if self.headers.get('Range') else 200)
                        self.send_header('Content-Type', entry['mime'])
                        self.send_header('Accept-Ranges', 'bytes')
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.send_header('Content-Length', str(last - first + 1))
                        if self.headers.get('Range'):
                            self.send_header('Content-Range', f'bytes {first}-{last}/{size}')
                        self.end_headers()
                        if body:
                            stream.seek(first)
                            remaining = last - first + 1
                            while remaining > 0 and not owner.closed.is_set() and owner.entry is entry:
                                chunk = stream.read(min(256 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                    pass  # Seeking closes previous browser requests.
                except OSError:
                    logger.exception('Source playback read failed')
                    self.close_connection = True
                finally:
                    owner.slots.release()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.host = f'127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':0.1},
                                       name='source-media', daemon=True)
        self.thread.start()

    def grant(self, path: Path):
        signature = identity(path.stat())
        with self.lock:
            if self.closed.is_set():
                raise ValueError('error.app_closing')
            if not self.entry or self.entry['path'] != path or self.entry['identity'] != signature:
                mime = {'.mp4':'video/mp4', '.m4v':'video/mp4', '.mov':'video/quicktime',
                        '.webm':'video/webm', '.mkv':'video/x-matroska', '.avi':'video/x-msvideo'}
                self.entry = {'path':path, 'identity':signature, 'route':'/' + secrets.token_urlsafe(32),
                              'mime':mime.get(path.suffix.lower(), 'application/octet-stream')}
            return 'http://' + self.host + self.entry['route']

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        with self.lock:
            self.entry = None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
