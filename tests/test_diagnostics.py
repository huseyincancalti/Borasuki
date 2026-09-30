"""Support packages preserve evidence, not private paths or arbitrary files."""

import json
import zipfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_queue
from borasuki.app import API
from borasuki.diagnostics import LOG_LIMIT


class DiagnosticsTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp
    create = test_queue.QueueTests.create

    def test_export_filters_strings_before_json_serialization(self):
        identifier = self.create()
        source = str(self.source)
        cause = f'Error from "{source}"\nAuthorization: Bearer secret123\nFrame: 38'
        self.service.update(identifier, status='failed', error='error.frame_transfer',
                            failure={'code':'error.frame_transfer', 'detail':cause})
        work = Path(self.service.jobs[identifier]['work'])
        work.mkdir(parents=True, exist_ok=True)
        (work / 'render.log').write_text(cause)
        (work / 'must-not-export.mp4').write_bytes(b'private video')
        (self.service.data / 'borasuki.log').write_bytes(b'x' * (LOG_LIMIT * 2) + cause.encode())
        before = self.service.store.load()
        path = self.service.export_diagnostics(str(self.root))
        with zipfile.ZipFile(path) as archive:
            report = json.loads(archive.read('report.json'))
            self.assertEqual(report['jobs'][0]['failure']['code'], 'error.frame_transfer')
            text = '\n'.join(archive.read(name).decode() for name in archive.namelist())
            for private in (source, self.source.name, 'secret123', 'private video'):
                self.assertNotIn(private, text)
            self.assertIn('Frame: 38', text)
            self.assertLessEqual(len(archive.read('application.log')), LOG_LIMIT)
            self.assertFalse(any(name.endswith(('.mp4', '.sqlite3', '.onnx')) for name in archive.namelist()))
        self.assertEqual(self.service.store.load(), before)
        self.assertFalse(list(self.root.glob('.borasuki-diagnostics-*')))

    def test_failed_publication_cleans_temp_and_preserves_existing(self):
        existing = self.root / 'Borasuki-diagnostics-aaaaaaaaaaaa.zip'
        existing.write_bytes(b'keep')
        with patch('borasuki.diagnostics.uuid.uuid4', return_value=Mock(hex='a' * 32)):
            with self.assertRaisesRegex(ValueError, 'error.diagnostics_export'):
                self.service.export_diagnostics(str(self.root))
        self.assertEqual(existing.read_bytes(), b'keep')
        self.assertFalse(list(self.root.glob('.borasuki-diagnostics-*')))

    def test_missing_logs_are_reported_and_picker_cancel_does_not_export(self):
        path = self.service.export_diagnostics(str(self.root))
        with zipfile.ZipFile(path) as archive:
            self.assertIn('application.log', json.loads(archive.read('report.json'))['omitted_logs'])
        api = API(self.service)
        api._window = Mock()
        api._window.create_file_dialog.return_value = None
        with patch.object(self.service, 'export_diagnostics') as export:
            self.assertEqual(api.export_diagnostics(), {'ok': True, 'data': None})
            export.assert_not_called()

    def test_unsafe_job_path_is_not_read(self):
        identifier = self.create()
        self.service.update(identifier, status='failed', work=str(self.root))
        (self.root / 'render.log').write_text('private unrelated data')
        path = self.service.export_diagnostics(str(self.root))
        with zipfile.ZipFile(path) as archive:
            self.assertNotIn('job-1/render.log', archive.namelist())
            self.assertIn('job-1: unsafe or unavailable folder', json.loads(archive.read('report.json'))['omitted_logs'])
