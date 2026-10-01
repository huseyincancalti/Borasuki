"""Publish only an explicitly requested, verified pre-release artifact."""

import argparse
import json
import subprocess
from pathlib import Path

from release_gate import ROOT, sha256


def validate(build):
    report = json.loads((build / 'release-verification.json').read_text(encoding='utf-8'))
    required = ('passed', 'source_gate', 'installed_files_identical', 'external_imports',
                'mux_frames_and_timing', 'installed_queue_and_preview', 'frozen_startup_shutdown')
    if not all(report.get(key) is True for key in required) or len(set(report.get('ffmpeg_matrix', []))) < 2:
        raise ValueError('Complete release gate evidence is required.')
    name = report['installer']
    if Path(name).name != name:
        raise ValueError('Invalid installer name')
    installer = build / 'installer' / name
    if sha256(installer) != report['sha256']:
        raise ValueError('Installer changed after verification.')
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).strip():
        raise ValueError('Commit the verified source before publishing.')
    tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=ROOT, text=True).strip()
    if tree != report['source_tree']:
        raise ValueError('Source changed after verification.')
    return installer, report


def main(build, tag):
    installer, report = validate(Path(build).resolve(strict=True))
    namespace = {}
    exec((ROOT / 'borasuki/version.py').read_text(), namespace)
    if tag != 'v' + namespace['VERSION'] or installer.name != f"Borasuki-Setup-{namespace['VERSION']}-win64.exe":
        raise ValueError('Tag, application and installer versions must match.')
    remote = subprocess.check_output(['git', 'remote', 'get-url', 'origin'], cwd=ROOT, text=True).strip()
    if remote not in ('https://github.com/huseyincancalti/Borasuki.git', 'git@github.com:huseyincancalti/Borasuki.git'):
        raise ValueError('Publish only from the clean public repository.')
    subprocess.run(['git', 'push', 'origin', 'HEAD'], cwd=ROOT, check=True)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    notes = (ROOT / 'RELEASE_NOTES.md').read_text(encoding='utf-8')
    notes += '\nInstaller SHA-256: `' + report['sha256'] + '`\n'
    subprocess.run(['gh', 'release', 'create', tag, str(installer), '--repo', 'huseyincancalti/Borasuki',
                    '--target', commit, '--prerelease', '--title', 'Borasuki ' + namespace['VERSION'],
                    '--notes-file', '-'], cwd=ROOT, input=notes, text=True, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run only after explicit user publication approval.')
    parser.add_argument('build')
    parser.add_argument('tag')
    args = parser.parse_args()
    main(args.build, args.tag)
