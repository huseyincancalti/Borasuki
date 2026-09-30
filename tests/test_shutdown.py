"""Close only after workers release their processes and persist recovery state."""

import copy
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

import test_queue
from borasuki.app import API
from borasuki.process import Interrupted, ProcessGroup


class ShutdownTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp
    create = test_queue.QueueTests.create
    reload = test_queue.QueueTests.reload

    def test_confirmation_does_not_mutate_running_or_queued_jobs(self):
        first, second = self.create('first'), self.create('second')
        self.service.active_id = first
        self.service.update(first, status='running')
        before = copy.deepcopy(self.service.jobs)
        with self.assertRaisesRegex(ValueError, 'error.confirmation'):
            self.service.begin_shutdown()
        self.assertEqual(self.service.jobs, before)
        self.assertFalse(self.service.closing)
        self.assertFalse(self.service.stop.is_set())

    def test_real_child_stops_and_saved_job_recovers_without_pausing_queued_jobs(self):
        first, second = self.create('first'), self.create('second')
        started = threading.Event()
        children = []
        before = copy.deepcopy(self.service.jobs[first]['configuration'])

        def render(job, stop, update):
            with ProcessGroup(stop) as group:
                children.append(group.spawn([sys.executable, '-c', 'import time; time.sleep(60)']))
                started.set()
                if not stop.wait(3):
                    raise AssertionError('Close signal missing')
                raise Interrupted()

        with patch('borasuki.service.execute', side_effect=render) as execute:
            self.service.worker.start()
            self.assertTrue(started.wait(3))
            self.service.shutdown()
            execute.assert_called_once()
        self.assertIsNotNone(children[0].poll())
        self.assertFalse(self.service.worker.is_alive())
        restored = self.reload()
        for identifier in (first, second):
            self.assertEqual(restored.jobs[identifier]['status'], 'queued')
        self.assertEqual(restored.jobs[first]['configuration'], before)
        self.assertTrue(restored.jobs[first]['recover_published'])
        self.assertEqual(self.source.read_bytes(), b'source')

    def test_timeout_keeps_window_open_until_worker_finishes(self):
        self.create()
        started, release = threading.Event(), threading.Event()

        def render(*args):
            started.set()
            release.wait(3)
            raise Interrupted()

        with patch('borasuki.service.execute', side_effect=render):
            self.service.worker.start()
            self.assertTrue(started.wait(3))
            try:
                with self.assertRaisesRegex(TimeoutError, 'error.shutdown_timeout'):
                    self.service.shutdown(timeout=.01)
                api = API(self.service)
                api._window = MagicMock()
                with patch.object(self.service, 'shutdown', side_effect=TimeoutError('error.shutdown_timeout')):
                    api._finish_close()
                api._window.destroy.assert_not_called()
                self.assertFalse(api._closed_safely)
                self.assertIn('window.closeFailed', api._window.evaluate_js.call_args.args[0])
            finally:
                release.set()
                self.service.shutdown()
        api._finish_close()
        api._window.destroy.assert_called_once()
        self.assertTrue(api._on_closing())

    def test_shutdown_between_scheduler_iterations_cannot_select_next_job(self):
        identifier = self.create()
        with patch.object(self.service, 'expire_resume', side_effect=lambda: self.service.begin_shutdown(True)), \
             patch('borasuki.service.execute') as execute:
            self.service._loop()
        execute.assert_not_called()
        self.assertEqual(self.service.jobs[identifier]['status'], 'queued')
        self.assertIsNone(self.service.active_id)
        self.assertTrue(self.service.stop.is_set())

    def test_replaced_analysis_must_also_finish_before_close(self):
        started, release = threading.Event(), threading.Event()

        def analyze(config, stop, update=None):
            started.set()
            release.wait(3)
            raise Interrupted()

        with patch.object(self.service, '_analyze_config', side_effect=analyze):
            self.service.request_analysis(str(self.source))
            self.assertTrue(started.wait(3))
            self.service.request_analysis(str(self.source))
            try:
                with self.assertRaisesRegex(TimeoutError, 'error.shutdown_timeout'):
                    self.service.shutdown(timeout=.01)
            finally:
                release.set()
                self.service.shutdown()
        self.assertFalse(self.service.analysis_futures)

    def test_closing_rejects_new_jobs_and_api_commands_but_allows_status(self):
        identifier = self.create()
        self.service.begin_shutdown(True)
        before = copy.deepcopy(self.service.jobs)
        with self.assertRaisesRegex(ValueError, 'error.app_closing'):
            self.create('late')
        api = API(self.service)
        self.assertEqual(api.job_action(identifier, 'pause')['error'], 'error.app_closing')
        self.assertTrue(api.state()['ok'])
        self.assertEqual(self.service.jobs, before)

    def test_late_prepare_cannot_commit_after_shutdown(self):
        prepare = self.service._prepare

        def closing_prepare(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.service.begin_shutdown(True)
            return result

        with patch.object(self.service, '_prepare', side_effect=closing_prepare):
            with self.assertRaisesRegex(ValueError, 'error.app_closing'):
                self.create()
        self.assertFalse(self.service.jobs)

    def test_shutdown_preserves_pause_and_cancel_intent(self):
        for action, expected in (('pause', 'paused'), ('cancel', 'cancelled')):
            with self.subTest(action=action):
                service = self.reload()
                identifier = service.create(str(self.source), str(self.root / (action + '.mkv')), 'original', 0, {})
                started = threading.Event()

                def render(job, stop, update):
                    started.set()
                    if not stop.wait(3):
                        raise AssertionError('Stop signal missing')
                    raise Interrupted()

                with patch('borasuki.service.execute', side_effect=render):
                    service.worker.start()
                    self.assertTrue(started.wait(3))
                    service.action(identifier, action, confirmed=True)
                    service.shutdown()
                self.assertEqual(service.jobs[identifier]['status'], expected)

    def test_waiting_preview_is_cancelled_without_cancelling_saved_jobs(self):
        identifier = self.create()
        self.service.preview_task = {'id': 'preview', 'status': 'waiting', 'stop': threading.Event()}
        self.service.shutdown()
        self.assertEqual(self.service.preview_task['status'], 'cancelled')
        self.assertTrue(self.service.preview_task['stop'].is_set())
        self.assertEqual(self.service.jobs[identifier]['status'], 'queued')

    def test_close_waits_for_an_inflight_file_import(self):
        api = API(self.service)
        api._window = MagicMock()
        api._window.create_file_dialog.return_value = [str(self.source)]
        started, release = threading.Event(), threading.Event()

        def inspect(source):
            started.set()
            release.wait(3)
            return self.media

        with patch.object(self.service, 'inspect', side_effect=inspect):
            importer = threading.Thread(target=api.choose_file)
            importer.start()
            self.assertTrue(started.wait(3))
            try:
                self.assertTrue(api.close_app(confirmed=True)['ok'])
                self.assertEqual(api._pending_commands, 1)
                api._window.destroy.assert_not_called()
                self.assertFalse(api._closed_safely)
            finally:
                release.set()
                importer.join(timeout=3)
                api._close_thread.join(timeout=3)
        self.assertTrue(api._closed_safely)
        self.assertEqual(api._pending_commands, 0)
        api._window.destroy.assert_called_once()

    def test_close_before_frontend_ready_still_shuts_down_safely(self):
        api = API(self.service)
        api._window = MagicMock()
        api._window.evaluate_js.return_value = False
        api._ask_close()
        api._close_thread.join(timeout=3)
        self.assertTrue(api._closed_safely)
        self.assertTrue(self.service.closing)
        api._window.destroy.assert_called_once()
