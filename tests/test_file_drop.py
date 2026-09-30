"""Native-path handoff and import state regressions; no user files or GPU work."""

import json
import shutil
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

import test_queue
from borasuki.app import API
from borasuki.storage import ROOT


class FileDropTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp

    def api(self):
        api = API(self.service)
        api._window = Mock()
        api._window.evaluate_js.return_value = True
        return api

    def event(self, path=None):
        return {'dataTransfer': {'files': [{'name': 'source.mp4', 'pywebviewFullPath': path or str(self.source)}]}}

    def test_single_file_reserves_form_before_inspect_and_never_queues(self):
        api = self.api()
        def inspect(path):
            self.assertEqual(api._window.evaluate_js.call_args.args[0], 'window.beginFileImport()')
            self.assertEqual(api._pending_commands, 1)
            return self.media
        with patch.object(self.service, 'inspect', side_effect=inspect):
            self.assertTrue(api._drop(self.event())['ok'])
        script = api._window.evaluate_js.call_args.args[0]
        self.assertIn('window.finishFileImport({"media":', script)
        self.assertFalse(self.service.jobs)
        self.assertEqual(api._pending_commands, 0)

    def test_missing_or_partial_native_paths_report_recovery_without_guessing(self):
        api = self.api()
        for files in ([{'name': 'missing.mp4'}], [{'pywebviewFullPath': str(self.source)}, {'name':'missing.mp4'}],
                      [{'pywebviewFullPath': 'relative.mp4'}]):
            with patch.object(self.service, 'inspect') as inspect, self.assertLogs('borasuki.app', level='ERROR'):
                api._drop({'dataTransfer': {'files': files}})
                inspect.assert_not_called()
            script = api._window.evaluate_js.call_args.args[0]
            result = json.loads(script.removeprefix('window.finishFileImport(')[:-1])
            self.assertEqual(result['failure']['code'], 'error.drop_path')
            self.assertEqual(result['failure']['recovery'], 'source')

    def test_busy_or_editing_form_rejects_before_probe(self):
        api = self.api()
        api._window.evaluate_js.return_value = False
        with patch.object(self.service, 'inspect') as inspect:
            api._drop(self.event())
            inspect.assert_not_called()
        api._window.evaluate_js.assert_called_once_with('window.beginFileImport()')

    def test_probe_failure_finishes_loading_and_lock_can_be_reused(self):
        api = self.api()
        with patch.object(self.service, 'inspect', side_effect=ValueError('error.no_video')), self.assertLogs('borasuki.app', level='ERROR'):
            api._drop(self.event())
        self.assertIn('"code": "error.no_video"', api._window.evaluate_js.call_args.args[0])
        with patch.object(self.service, 'inspect', return_value=self.media):
            api._drop(self.event())
        self.assertIn('"media":', api._window.evaluate_js.call_args.args[0])

    def test_duplicate_drop_cannot_replace_inflight_import(self):
        api = self.api()
        entered, release = threading.Event(), threading.Event()
        def inspect(path):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.media
        with patch.object(self.service, 'inspect', side_effect=inspect) as probe:
            worker = threading.Thread(target=api._drop, args=(self.event(),))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                api._drop(self.event())
                self.assertEqual(probe.call_count, 1)
            finally:
                release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for frontend contract')
    def test_drag_feedback_and_bridge_timeout(self):
        subprocess.run(['node', str(ROOT / 'tests/file_drop_frontend.cjs')], check=True, timeout=10)
