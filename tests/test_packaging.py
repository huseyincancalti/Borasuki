"""Frozen bundle must also support the external VSPipe interpreter."""

import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_spec_external_imports(self):
        captured = {}

        def analysis(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

        with patch('pathlib.Path.cwd', return_value=ROOT):
            runpy.run_path(str(ROOT / 'Borasuki.spec'), init_globals={
                'Analysis': analysis, 'PYZ': lambda *a: None,
                'EXE': lambda *a, **k: None, 'COLLECT': lambda *a, **k: None})
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / 'Borasuki'
            for source, destination in captured['datas']:
                source = ROOT / source
                target = app / '_internal' / destination
                target.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, target)
            result = subprocess.run([sys.executable, '-I', str(ROOT / 'installer/check_bundle.py'), str(app)],
                                    cwd=folder, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_rejects_missing_dependency(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / '_internal/borasuki'
            target.mkdir(parents=True)
            shutil.copy2(ROOT / 'borasuki/__init__.py', target)
            result = subprocess.run([sys.executable, '-I', str(ROOT / 'installer/check_bundle.py'), folder],
                                    cwd=folder, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("No module named 'borasuki.storage'", result.stderr)
