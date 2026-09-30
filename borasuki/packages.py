"""Pinned package cache; downloading never activates or executes a runtime."""

import hashlib
import http.client
import os
import re
import shutil
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from borasuki.process import Interrupted


HOSTS = frozenset({'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com',
                   'www.python.org', 'files.pythonhosted.org'})
CHUNK = 64 * 1024
RESERVE = 16 * 1024 * 1024


def _url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError('error.package_source')
    return value


@dataclass(frozen=True)
class Package:
    name: str
    url: str
    size: int
    sha256: str

    def __post_init__(self):
        _url(self.url)
        if type(self.size) is not int or self.size <= 0 or not re.fullmatch('[0-9a-f]{64}', self.sha256):
            raise ValueError('error.package_manifest')


# R79: publisher release digest. CUGAN: locally verified archive and four pinned model hashes.
PACKAGES = {
    'python-3.13': Package('Python 3.13 runtime',
        'https://www.python.org/ftp/python/3.13.12/python-3.13.12-embed-amd64.zip',
        10941233, '76f238f606250c87c6beac75dccd35ee99070a13490555936abb6cb64ecce3d0'),
    'numpy-2.5': Package('NumPy 2.5',
        'https://files.pythonhosted.org/packages/10/70/800b3fca480af32df9e8ea9f3d4a0c8feb4b32d7f195d174eabbda4829ad/numpy-2.5.1-cp313-cp313-win_amd64.whl',
        12425674, '6c3fe51bc6a16453d452997053454f309e8e0ed7b42d6b361ce4ac8c32913d74'),
    'vapoursynth-r79': Package('VapourSynth R79',
        'https://github.com/vapoursynth/vapoursynth/releases/download/R79/VapourSynth64-Portable-R79.zip',
        23160674, '625b3410d903943107291592e90d6f521f829ebb8291d952ee91b8d674bbb153'),
    'cugan-v2': Package('Real-CUGAN models',
        'https://github.com/AmusementClub/vs-mlrt/releases/download/model-20211209/cugan_v2.7z',
        53776727, '1bd3f8bce70956c9c2963cc42cd8383fa9e91e596a6520236324ec1c54638836'),
    'ffms2-5.0': Package('FFMS2 5.0',
        'https://github.com/FFMS/ffms2/releases/download/5.0/ffms2-5.0-msvc.7z',
        8588155, 'e867a3df7262865107df40f230f5b8e1455905eba9b8852e6f35b1227537caeb'),
    'ffmpeg-8.1': Package('FFmpeg 8.1',
        'https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-09-29-13-10/ffmpeg-n8.1.3-6-gff48edd8b2-win64-gpl-8.1.zip',
        193110537, '6e3294ba26c4a21c267ca1dc8a029268b2b89ce61496d9d4be8ed820a42a0c27'),
    'vsmlrt-scripts': Package('vs-mlrt scripts',
        'https://github.com/AmusementClub/vs-mlrt/releases/download/v15.16/scripts.v15.16.7z',
        17732, 'd07dae0a00cb8dbf4f00358f640f630ff5d933de44d27050e7acce4f31cc3560'),
    'vstrt': Package('VSTRT plugin',
        'https://github.com/AmusementClub/vs-mlrt/releases/download/v15.16/VSTRT-Windows-x64.v15.16.7z',
        486704, 'c4e64e69e87553bf15a7acd29d76debd006954e39ee07ef0e6d526947c5d34b1'),
    'tensorrt-1': Package('TensorRT runtime (part 1/2)',
        'https://github.com/AmusementClub/vs-mlrt/releases/download/v15.16/vsmlrt-windows-x64-tensorrt.v15.16.7z.001',
        2147483647, '9fe674f62b9d33a369e7bd6584052986af4c81b12a82b75e5d82aad7c06733cb'),
    'tensorrt-2': Package('TensorRT runtime (part 2/2)',
        'https://github.com/AmusementClub/vs-mlrt/releases/download/v15.16/vsmlrt-windows-x64-tensorrt.v15.16.7z.002',
        528530525, '387b295726bd159f5b4965d0ee8d55bb377f1d8a2add7f623324c60cece44a16'),
    '7zr': Package('7-Zip extractor',
        'https://github.com/ip7z/7zip/releases/download/26.03/7zr.exe',
        602624, 'ad4c82fadcbdf93c03b4fc440f300509c7d60c5c2f4d183e35d9d70d6957037d'),
}


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        _url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def _safe(path):
    # Reject junctions/symlinks and hard-linked cache files before any mutation.
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('error.package_path')
        if current == path and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ValueError('error.package_path')
    return path


@contextmanager
def _lock(path):
    with _safe(path).open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('error.package_busy') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _check(stop, deadline):
    if stop.is_set():
        raise Interrupted()
    if time.monotonic() >= deadline:
        raise TimeoutError('error.package_timeout')


def _verify(path, package, stop, deadline):
    if not path.exists():
        return False
    if _safe(path).stat().st_size != package.size:
        return False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK):
            _check(stop, deadline)
            digest.update(chunk)
    return digest.hexdigest() == package.sha256


def _reject(path):
    # Keep evidence; never recursively delete or touch runtime/user files.
    if path.exists():
        target = path.with_name(path.name + '.' + uuid.uuid4().hex + '.rejected')
        _safe(path).rename(_safe(target))


def download(package, cache, stop, update=lambda **fields: None, *, budget=180):
    """One bounded attempt; explicit retry resumes the SHA-256-addressed partial."""
    if not isinstance(package, Package) or not 0 < budget <= 3600:
        raise ValueError('error.package_manifest')
    deadline = time.monotonic() + budget
    _check(stop, deadline)
    cache = _safe(Path(cache).absolute())
    cache.mkdir(parents=True, exist_ok=True)
    ready = _safe(cache / (package.sha256 + '.package'))
    partial = _safe(cache / (package.sha256 + '.partial'))
    with _lock(cache / (package.sha256 + '.lock')):
        update(stage='verifying', downloaded=0, total=package.size, package=package.name)
        if _verify(ready, package, stop, deadline):
            update(stage='verified', downloaded=package.size, total=package.size, package=package.name)
            return ready
        _reject(ready)
        offset = partial.stat().st_size if partial.exists() else 0
        if offset >= package.size:
            if _verify(partial, package, stop, deadline):
                _check(stop, deadline)
                os.replace(_safe(partial), _safe(ready))
                update(stage='verified', downloaded=package.size, total=package.size, package=package.name)
                return ready
            _reject(partial)
            offset = 0
        headers = {'User-Agent': 'Borasuki-runtime-setup', 'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        _check(stop, deadline)
        opener = urllib.request.build_opener(_Redirects())
        request = urllib.request.Request(_url(package.url), headers=headers)
        try:
            with opener.open(request, timeout=min(5, max(0.1, deadline - time.monotonic()))) as response:
                _url(response.url)
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise ValueError('error.package_response')
                if response.status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not match or tuple(map(int, match.groups())) != (offset, package.size - 1, package.size):
                        raise ValueError('error.package_response')
                elif response.status == 200:
                    offset = 0  # Range ignored: restart, never append a complete response.
                else:
                    raise ValueError('error.package_response')
                length = response.headers.get('Content-Length')
                if length is not None and length != str(package.size - offset):
                    raise ValueError('error.package_response')
                if shutil.disk_usage(cache).free < package.size - offset + RESERVE:
                    raise ValueError('error.disk_space')
                _check(stop, deadline)
                with _safe(partial).open('ab' if offset else 'wb') as stream:
                    update(stage='downloading', downloaded=offset, total=package.size, package=package.name)
                    while True:
                        _check(stop, deadline)
                        chunk = response.read1(CHUNK)
                        _check(stop, deadline)
                        if not chunk:
                            break
                        if offset + len(chunk) > package.size:
                            raise ValueError('error.package_response')
                        stream.write(chunk)
                        offset += len(chunk)
                        update(stage='downloading', downloaded=offset, total=package.size, package=package.name)
                    stream.flush()
                    os.fsync(stream.fileno())
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            error = ValueError('error.package_http')
            error.add_note(f'{package.name}: HTTP {code}')
            raise error from None
        except (urllib.error.URLError, ConnectionError, http.client.HTTPException, ssl.SSLError) as exc:
            error = ConnectionError('error.package_network')
            reason = getattr(exc, 'reason', exc)
            error.add_note(f'{package.name}: {type(reason).__name__}')
            raise error from None
        except TimeoutError as exc:
            raise TimeoutError('error.package_timeout') from exc
        if offset != package.size:
            raise ValueError('error.package_incomplete')
        update(stage='verifying', downloaded=offset, total=package.size, package=package.name)
        if not _verify(partial, package, stop, deadline):
            _reject(partial)
            raise ValueError('error.package_integrity')
        _check(stop, deadline)
        os.replace(_safe(partial), _safe(ready))
        update(stage='verified', downloaded=offset, total=package.size, package=package.name)
        return ready
