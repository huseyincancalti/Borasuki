# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project = Path.cwd()
renderer_files = [(str(path), 'borasuki') for pattern in ('*.py', '*.vpy', '*.ps1')
                  for path in sorted((project / 'borasuki').glob(pattern))]
a = Analysis(
    ['main.py'],
    pathex=[str(project)],
    binaries=[],
    datas=[('frontend', 'frontend'),
           ('borasuki/locales', 'borasuki/locales'),
           *renderer_files],
    hiddenimports=['webview.platforms.edgechromium', 'webview.platforms.winforms'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Borasuki',
    console=False,
    icon=str(project / 'frontend/assets/img/logo.ico'),
)
coll = COLLECT(exe, a.binaries, a.datas, name='Borasuki')
