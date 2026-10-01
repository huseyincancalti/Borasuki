import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from borasuki.storage import ROOT


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.build = Path(temporary.name)
        (self.build / 'installer').mkdir()
        self.installer = self.build / 'installer/test.exe'
        self.installer.write_bytes(b'fixture, not an installer')
        with patch.object(sys, 'path', [str(ROOT / 'installer'), *sys.path]):
            spec = importlib.util.spec_from_file_location('release_publish', ROOT / 'installer/publish.py')
            self.publish = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.publish)
        self.report = {key: True for key in ('passed', 'source_gate', 'installed_files_identical', 'external_imports',
                        'mux_frames_and_timing', 'installed_queue_and_preview', 'frozen_startup_shutdown')}
        self.report.update(installer='test.exe', sha256=self.publish.sha256(self.installer),
                           ffmpeg_matrix=['7.1.1', '8.1.3'], source_tree='verified-tree')
        self.git = patch.object(self.publish.subprocess, 'check_output', side_effect=[b'', 'verified-tree\n'])

    def write(self):
        (self.build / 'release-verification.json').write_text(json.dumps(self.report))

    def test_missing_receipt_blocks_publication(self):
        with self.assertRaises(FileNotFoundError):
            self.publish.validate(self.build)

    def test_missing_gate_or_single_runtime_blocks_publication(self):
        for key in ('passed', 'installed_queue_and_preview', 'source_gate'):
            self.report[key] = False
            self.write()
            with self.assertRaisesRegex(ValueError, 'evidence'):
                self.publish.validate(self.build)
            self.report[key] = True
        self.report['ffmpeg_matrix'] = ['7.1.1', '7.1.1']
        self.write()
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.publish.validate(self.build)

    def test_changed_installer_is_rejected(self):
        self.write()
        self.installer.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Installer changed'):
            self.publish.validate(self.build)

    def test_dirty_or_changed_source_is_rejected(self):
        self.write()
        for output, message in (([b' M file'], 'Commit'), ([b'', 'different-tree'], 'Source changed')):
            with patch.object(self.publish.subprocess, 'check_output', side_effect=output), self.assertRaisesRegex(ValueError, message):
                self.publish.validate(self.build)

    def test_exact_clean_verified_artifact_is_accepted(self):
        self.write()
        with self.git:
            installer, report = self.publish.validate(self.build)
        self.assertEqual(installer, self.installer)
        self.assertEqual(report, self.report)

    def test_gate_uses_pinned_embedded_python_layout_and_rejects_missing_executable(self):
        from release_gate import embedded_python
        with self.assertRaises(FileNotFoundError):
            embedded_python(self.build)
        python = self.build / 'python/python.exe'
        python.parent.mkdir()
        python.write_bytes(b'fixture')
        self.assertEqual(embedded_python(self.build), python.resolve())
