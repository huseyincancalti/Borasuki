"""Fail-closed checks for the exact installer, in an isolated data directory."""

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args, timeout=1200, **kwargs):
    subprocess.run(args, cwd=ROOT, check=True, timeout=timeout, **kwargs)


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def embedded_python(runtime):
    return (Path(runtime) / 'python/python.exe').resolve(strict=True)


def source_gate(build):
    subprocess.run(['git', 'diff', '--exit-code'], cwd=ROOT, check=True, capture_output=True)
    if subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard'], cwd=ROOT).strip():
        raise ValueError('Stage intended files; untracked files are not allowed in a release build.')
    tree = subprocess.check_output(['git', 'write-tree'], cwd=ROOT, text=True).strip()
    run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'])
    for path in sorted((ROOT / 'tests').glob('*_frontend.cjs')):
        run(['node', str(path)], timeout=60)
    import os
    run([sys.executable, '-m', 'tests.integration_desktop'], timeout=90,
        env={**os.environ, 'PYTHONPATH': str(ROOT) + os.pathsep + str(ROOT / 'tests')})
    folder = Path(build).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'diff', '--exit-code'], cwd=ROOT, check=True, capture_output=True)
    if subprocess.check_output(['git', 'write-tree'], cwd=ROOT, text=True).strip() != tree:
        raise ValueError('Source changed during verification; discard this test run.')
    (folder / 'source-verification.json').write_text(json.dumps({'passed': True, 'tree': tree}), encoding='utf-8')


def artifact_gate(build, runtime, compatibility, engine_cache):
    build = Path(build).resolve(strict=True)
    source = json.loads((build / 'source-verification.json').read_text(encoding='utf-8'))
    tree = subprocess.check_output(['git', 'write-tree'], cwd=ROOT, text=True).strip()
    if source.get('passed') is not True or source.get('tree') != tree:
        raise ValueError('Source tests must pass for this exact source tree first.')
    runtime = Path(runtime).resolve(strict=True)
    compatibility = Path(compatibility).resolve(strict=True)
    if runtime == compatibility:
        raise ValueError('Two distinct FFmpeg runtime paths are required.')
    installer, = (build / 'installer').glob('Borasuki-Setup-*-win64.exe')
    app = build / 'verification-install'
    if not app.exists():
        subprocess.run([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                        '/BORASUKIVERIFY=1', f'/DIR={app}'], check=True, timeout=120)
    expected = build / 'dist/Borasuki'
    for path in expected.rglob('*'):
        if path.is_file():
            copied = app / path.relative_to(expected)
            if not copied.is_file() or sha256(copied) != sha256(path):
                raise ValueError('Installed file differs from verified build: ' + str(path.relative_to(expected)))
    tracked = subprocess.check_output(['git', 'ls-files', 'borasuki', 'frontend'], cwd=ROOT, text=True).splitlines()
    for path in (app / '_internal/frontend').rglob('*'):
        if path.is_file() and path.relative_to(app / '_internal').as_posix() not in tracked:
            raise ValueError('Untracked frontend file in installer: ' + path.name)
    for relative in tracked:
        if sha256(ROOT / relative) != sha256(app / '_internal' / relative):
            raise ValueError('Application source changed after build: ' + relative)
    run([sys.executable, '-I', str(ROOT / 'installer/check_bundle.py'), str(app)])
    runtimes = []
    for folder in (compatibility, runtime):
        version = subprocess.check_output([str(folder / 'ffmpeg.exe'), '-version'], text=True).splitlines()[0]
        runtimes.append(version)
        run([sys.executable, '-I', str(ROOT / 'tests/integration_mux.py'), str(folder), str(app)])
    embedded = embedded_python(runtime)
    attempt = uuid.uuid4().hex[:8]
    gpu = [str(embedded), '-I', str(ROOT / 'tests/integration_installed.py'), str(app), str(runtime), str(build / f'installed-test-{attempt}')]
    if engine_cache:
        gpu += ['--engine-cache', str(Path(engine_cache).resolve(strict=True))]
    run(gpu)
    run([sys.executable, str(ROOT / 'tests/integration_frozen_startup.py'), str(app), str(build / f'startup-test-{attempt}'), str(runtime)], timeout=120)
    report = {'passed': True, 'installer': installer.name, 'sha256': sha256(installer),
              'source_gate': True, 'installed_files_identical': True, 'external_imports': True,
              'ffmpeg_matrix': runtimes, 'mux_frames_and_timing': True,
              'installed_queue_and_preview': True, 'frozen_startup_shutdown': True,
              'source_tree': subprocess.check_output(['git', 'write-tree'], cwd=ROOT, text=True).strip()}
    (build / 'release-verification.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('Release gate passed. Installer SHA-256: ' + report['sha256'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', action='store_true')
    parser.add_argument('--build')
    parser.add_argument('--runtime')
    parser.add_argument('--compatibility')
    parser.add_argument('--engine-cache')
    args = parser.parse_args()
    if args.source:
        if not args.build:
            parser.error('--source requires --build for the verification receipt.')
        source_gate(args.build)
    elif args.build and args.runtime and args.compatibility:
        artifact_gate(args.build, args.runtime, args.compatibility, args.engine_cache)
    else:
        parser.error('Provide --source, or --build, --runtime and --compatibility.')
