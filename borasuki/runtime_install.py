"""Build an isolated Python/VapourSynth runtime from verified archives."""

import io
import os
import shutil
import subprocess
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from borasuki.process import Interrupted
from borasuki.packages import PACKAGES, download
from borasuki.runtime import required_files, validate_models, validate_runtime


def _member_path(name):
    path = PurePosixPath(name)
    if (not name or name.startswith(('/', '\\')) or '\\' in name or ':' in name
            or any(part in ('', '.', '..') for part in path.parts)):
        raise ValueError('error.package_archive')
    return path


def extract_zip(source, destination, stop, *, prefix=None):
    """Extract only regular ZIP members beneath destination, rejecting links/traversal."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            if stop.is_set():
                raise Interrupted()
            path = _member_path(member.filename)
            if prefix:
                parts = PurePosixPath(prefix).parts
                if path.parts[:len(parts)] != parts:
                    continue
                path = PurePosixPath(*path.parts[len(parts):])
                if not path.parts:
                    continue
            mode = (member.external_attr >> 16) & 0xFFFF
            if mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError('error.package_archive')
            target = destination.joinpath(*path.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as original, target.open('xb') as output:
                while chunk := original.read(1024 * 1024):
                    if stop.is_set():
                        raise Interrupted()
                    output.write(chunk)


def install_python(archives, stage, stop):
    """Stage CPython, NumPy and the VapourSynth wheel without touching system Python."""
    python = Path(stage) / 'python'
    extract_zip(archives['python-3.13'], python, stop)
    site = python / 'Lib' / 'site-packages'
    extract_zip(archives['numpy-2.5'], site, stop)
    with zipfile.ZipFile(archives['vapoursynth-r79']) as outer:
        wheels = [name for name in outer.namelist() if name.startswith('wheel/vapoursynth-') and name.endswith('.whl')]
        if len(wheels) != 1:
            raise ValueError('error.package_archive')
        with outer.open(wheels[0]) as stream:
            extract_zip(io.BytesIO(stream.read()), site, stop)
    for filename in ('vspipe.exe', 'libvapoursynth.dll', 'vsscript.dll'):
        if not (site / 'vapoursynth' / filename).is_file():
            raise ValueError('error.package_archive')
    pth = python / 'python313._pth'
    if not pth.is_file():
        raise ValueError('error.package_archive')
    pth.write_text('python313.zip\n.\nLib\\site-packages\nimport site\n', encoding='utf-8')
    return python


def extract_7z(extractor, source, destination, stop):
    """Unpack a pinned 7z asset after checking its member paths."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    listing = subprocess.run([str(extractor), 'l', '-slt', str(source)], capture_output=True,
                             check=True, timeout=90)
    lines = listing.stdout.decode('utf-8', errors='replace').splitlines()
    marker = next((index for index, line in enumerate(lines) if line.startswith('----------')), None)
    if marker is None:
        raise ValueError('error.package_archive')
    for line in lines[marker + 1:]:
        if line.startswith('Path = '):
            _member_path(line[7:].replace('\\', '/'))
        if line.startswith(('Symbolic Link = ', 'Hard Link = ')):
            raise ValueError('error.package_archive')
    if stop.is_set():
        raise Interrupted()
    process = subprocess.Popen([str(extractor), 'x', '-y', '-bd', '-bso0', '-bsp0',
                                f'-o{destination}', str(source)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE)
    deadline = time.monotonic() + 3600
    try:
        while process.poll() is None:
            if stop.wait(0.2):
                process.kill()
                raise Interrupted()
            if time.monotonic() >= deadline:
                process.kill()
                raise TimeoutError('error.package_timeout')
        error = process.stderr.read().decode('utf-8', errors='replace')
        if process.returncode:
            raise ValueError(f'error.package_archive: 7-Zip exit {process.returncode}: {error[-300:]}')
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stderr.close()
    for root, folders, files in os.walk(destination, followlinks=False):
        for name in [*folders, *files]:
            path = Path(root) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('error.package_archive')


def _stage_runtime(archives, stage, stop):
    stage = Path(stage)
    stage.mkdir(parents=True)
    plugins = stage / 'plugins'
    plugins.mkdir()
    extractor = stage / '7zr.exe'
    shutil.copy2(archives['7zr'], extractor)
    install_python(archives, stage, stop)
    extract_7z(extractor, archives['cugan-v2'], plugins / 'models', stop)
    extract_7z(extractor, archives['vsmlrt-scripts'], plugins, stop)
    temp = stage / 'components'
    temp.mkdir()
    extract_7z(extractor, archives['ffms2-5.0'], temp / 'ffms2', stop)
    shutil.copy2(temp / 'ffms2' / 'ffms2-5.0-msvc' / 'x64' / 'ffms2.dll', plugins / 'ffms2.dll')
    extract_zip(archives['ffmpeg-8.1'], temp / 'ffmpeg', stop)
    for filename in ('ffmpeg.exe', 'ffprobe.exe'):
        matches = list((temp / 'ffmpeg').glob(f'*/bin/{filename}'))
        if len(matches) != 1:
            raise ValueError('error.package_archive')
        shutil.copy2(matches[0], stage / filename)
    multipart = temp / 'tensorrt'
    multipart.mkdir()
    stem = 'vsmlrt-windows-x64-tensorrt.v15.16.7z'
    for part in (1, 2):
        os.link(archives[f'tensorrt-{part}'], multipart / f'{stem}.{part:03}')
    extract_7z(extractor, multipart / f'{stem}.001', plugins, stop)
    extract_7z(extractor, archives['vstrt'], plugins, stop)
    for path in required_files({'plugins': str(plugins), 'vspipe': str(stage / 'python/Lib/site-packages/vapoursynth/vspipe.exe')} ).values():
        if not path.is_file() or not path.stat().st_size:
            raise ValueError('error.runtime_missing')
    validate_models({'plugins': str(plugins)}, stop)
    if not temp.resolve().is_relative_to(stage.resolve()) or not stage.name.startswith('runtime-staging-'):
        raise ValueError('error.package_path')
    shutil.rmtree(temp)
    return stage


def install_runtime(data, stop, update, gpus, *, cache=None):
    """Download into a local cache, validate staged files, then activate atomically."""
    data = Path(data)
    if not gpus:
        raise ValueError('error.unsupported_gpu')
    cache = Path(cache) if cache else data / 'package-cache'
    total = sum(package.size for package in PACKAGES.values())
    if shutil.disk_usage(data).free < max(total + 4_000_000_000, 7_000_000_000):
        raise ValueError('error.disk_space')
    archives = {}
    completed = 0
    for key, package in PACKAGES.items():
        if stop.is_set():
            raise Interrupted()
        def progress(**fields):
            update(stage='download', package=package.name, downloaded=completed + fields['downloaded'],
                   total=total, package_stage=fields['stage'])
        archives[key] = download(package, cache, stop, progress, budget=3600)
        completed += package.size
    stage = data / f'runtime-staging-{uuid.uuid4().hex}'
    update(stage='extract', package=None, downloaded=total, total=total)
    try:
        _stage_runtime(archives, stage, stop)
        candidate = {'plugins': str(stage / 'plugins'),
                     'vspipe': str(stage / 'python/Lib/site-packages/vapoursynth/vspipe.exe'),
                     'ffmpeg': str(stage / 'ffmpeg.exe'), 'ffprobe': str(stage / 'ffprobe.exe'),
                     'gpus': gpus, 'missing': [], 'ready': True}
        validate_runtime(candidate, data, stop, lambda **fields: update(**fields))
    except Exception:
        if stage.is_dir() and stage.name.startswith('runtime-staging-') and stage.resolve().is_relative_to(data.resolve()):
            shutil.rmtree(stage)
        raise
    target = data / 'runtime'
    backup = None
    if target.exists():
        backup = data / f'runtime-backup-{uuid.uuid4().hex}'
        os.replace(target, backup)
    try:
        os.replace(stage, target)
    except OSError:
        if backup and not target.exists():
            os.replace(backup, target)
        raise
    update(stage='installed', package=None)
    return {**candidate, 'plugins': str(target / 'plugins'),
            'vspipe': str(target / 'python/Lib/site-packages/vapoursynth/vspipe.exe'),
            'ffmpeg': str(target / 'ffmpeg.exe'), 'ffprobe': str(target / 'ffprobe.exe')}
