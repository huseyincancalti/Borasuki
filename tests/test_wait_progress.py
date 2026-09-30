import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from borasuki.process import ProcessGroup
from borasuki.storage import atomic_json
from borasuki.wait_progress import WaitProgress


class WaitProgressTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.updates = []
        clock = patch('borasuki.wait_progress.time.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def tracker(self, path=None):
        return WaitProgress(lambda **fields: self.updates.append(fields['progress']), path)

    def test_unknown_build_has_no_fake_percentage_or_eta(self):
        progress = self.tracker()
        progress.set('engine_building')
        self.now = 70
        progress.publish()
        self.assertIsNone(self.updates[-1]['total'])
        self.assertIsNone(self.updates[-1]['eta'])
        self.assertEqual(self.updates[-1]['elapsed'], 70)

    def test_sample_eta_waits_for_measurements_and_resets_on_phase_change(self):
        progress = self.tracker()
        progress.set('color_sampling', 0, 9)
        self.now = 4
        progress.set('color_sampling', 1, 9)
        self.assertIsNone(self.updates[-1]['eta'])
        self.now = 8
        progress.set('color_sampling', 3, 9)
        self.assertEqual(self.updates[-1]['eta'], 12)
        self.now = 9
        progress.set('verification')
        self.assertIsNone(self.updates[-1]['eta'])
        self.assertIsNone(self.updates[-1]['completed'])

    def test_history_saved_only_after_success_and_overrun_is_not_zero_eta(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.json'
            progress = self.tracker(path)
            progress.set('engine_building')
            self.now = 30
            self.assertFalse(path.exists(), 'failure/cancellation must not save durations')
            progress.finish()
            second = self.tracker(path)
            second.set('engine_building')
            self.assertEqual(self.updates[-1]['eta'], 30)
            self.assertEqual(self.updates[-1]['eta_basis'], 'history')
            self.now = 65
            second.publish()
            self.assertIsNone(self.updates[-1]['eta'])
            self.assertIsNone(self.updates[-1]['total'])

    def test_incremental_file_poll_does_not_read_stale_previous_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'progress.json'
            progress = self.tracker()
            progress.set('source_reading')
            self.now = 1
            progress.poll(path)
            atomic_json(path, {'stage':'color_sampling', 'completed':2, 'total':9})
            self.now = 2
            progress.poll(path)
            self.assertEqual(self.updates[-1]['completed'], 2)
            self.assertEqual(self.updates[-1]['total'], 9)

    def test_invalid_history_does_not_block_preparation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.json'
            atomic_json(path, {'engine_building':'bad'})
            with self.assertLogs('borasuki.wait_progress', level='WARNING'):
                progress = self.tracker(path)
            progress.set('engine_building')
            self.assertIsNone(self.updates[-1]['eta'])

    def test_process_poll_is_optional_and_does_not_leak_to_popen(self):
        import sys
        calls = []
        with ProcessGroup(threading.Event()) as group:
            output = group.capture([sys.executable, '-c', 'print("ok")'],
                                   on_poll=lambda: calls.append(True), timeout=5)
        self.assertEqual(output.strip(), b'ok')
        self.assertTrue(calls)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_shared_frontend_long_wait_and_reset(self):
        subprocess.run([shutil.which('node'), str(Path(__file__).with_name('wait_progress_frontend.cjs'))],
                       check=True, timeout=10, capture_output=True)
