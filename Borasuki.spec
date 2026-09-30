# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project = Path.cwd()
a = Analysis(
    ['main.py'],
    pathex=[str(project)],
    binaries=[],
    datas=[('frontend', 'frontend'),
           ('borasuki/locales', 'borasuki/locales'),
           ('borasuki/render.vpy', 'borasuki'),
           ('borasuki/runtime_check.vpy', 'borasuki')],
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
