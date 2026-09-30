"""Batch preparation preserves per-file snapshots and queue lifecycle rules."""

import copy
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_queue
from borasuki.app import API
from borasuki.batch import collect
from borasuki.process import Interrupted


class BatchTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp
    reload = test_queue.QueueTests.reload

    def recipe(self, mode='custom'):
        return {'upscale': {'scale': 2}, 'denoise': {'enabled': False, 'strength': 3},
                'color_mode': mode, 'custom': {'contrast': 1.02, 'brightness': 0.01, 'saturation': 1.01},
                'adaptive_profile': 'normal', 'color_overrides': {'brightness': 0.01}}

    def sources(self):
        other = self.root / 'second.mp4'
        other.write_bytes(b'other video')
        return [str(self.source), str(other)]

    def start(self, paths=None, recipe=None, preset=None):
        token = self.service.request_batch(paths or self.sources(), str(self.root), recipe or self.recipe(), 1, preset)
        self.assertEqual(self.service.batch_task['id'], token)
        return self.service.batch_task['future']

    def test_selection_deduplicates_and_does_not_recurse_or_enqueue(self):
        paths = self.sources()
        nested = self.root / 'nested'
        nested.mkdir()
        (nested / 'ignored.mp4').write_bytes(b'nested')
        (self.root / 'notes.txt').write_text('not a video')
        result = collect([str(self.root), paths[0], paths[0].upper()])
        self.assertEqual(result['paths'], sorted(paths, key=str.casefold))
        self.assertEqual(result['failures'], [])
        self.assertEqual(self.service.jobs, {})

    def test_batch_mp4_requires_single_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, 'error.mp4_confirm_required'):
            self.service.request_batch([str(self.source)], str(self.root), self.recipe(), 1,
                                       output_format='mp4')
        self.media['streams'] = ['audio', 'subtitle']
        self.media['track_codecs'] = [{'type':'audio', 'codec':'aac'}, {'type':'subtitle', 'codec':'ass'}]
        token = self.service.request_batch([str(self.source)], str(self.root), self.recipe(), 1,
                                           output_format='mp4', drop_tracks_confirmed=True)
        self.service.batch_task['future'].result(5)
        self.assertEqual(self.service.batch_task['id'], token)
        self.assertEqual(self.service.batch_task['items'][0]['status'], 'added')
        job = next(iter(self.service.jobs.values()))
        self.assertEqual(Path(job['output']).suffix, '.mp4')
        self.assertTrue(job['drop_tracks_confirmed'])

    def test_selection_reports_missing_and_invalid_files_and_limits(self):
        result = collect([str(self.source), str(self.root / 'missing.mp4'), str(self.root / 'vspipe.exe')])
        self.assertEqual(result['paths'], [str(self.source)])
        self.assertEqual([item['failure']['code'] for item in result['failures']], ['error.source_access', 'error.batch_format'])
        for invalid in ([], 'path', [None], [str(self.source)] * 201):
            with self.assertRaisesRegex(ValueError, 'error.batch_selection'):
                collect(invalid)
        videos = self.root / 'many'
        videos.mkdir()
        for index in range(201):
            (videos / f'{index}.mp4').touch()
        with self.assertRaisesRegex(ValueError, 'error.batch_limit'):
            collect([str(videos)])

    def test_each_job_persists_snapshot_and_output_name(self):
        paths = self.sources()
        self.start(paths).result(5)
        task = self.service.snapshot()['batch']
        self.assertEqual(task['status'], 'complete')
        self.assertEqual([item['status'] for item in task['items']], ['added', 'added'])
        for job in self.service.jobs.values():
            self.assertEqual(job['gpu_id'], 1)
            self.assertEqual(job['grade'], self.recipe()['custom'])
            self.assertFalse(job['denoise']['enabled'])
            self.assertEqual(job['status'], 'queued')
            self.assertTrue(job['output'].endswith('_borasuki_2x.mkv'))
            self.assertEqual(job['configuration']['grade'], job['grade'])
        self.assertEqual(self.reload().jobs.keys(), self.service.jobs.keys())
        self.assertNotIn('future', task)
        self.assertNotIn('stop', task)

    def test_bad_file_and_output_conflict_do_not_rollback_good_file(self):
        paths = self.sources()
        output = self.root / 'source_borasuki_2x.mkv'
        output.write_bytes(b'keep output')
        with self.assertLogs('borasuki.service', level='ERROR'):
            self.start(paths + [str(self.root / 'missing.mp4')]).result(5)
        self.assertEqual(len(self.service.jobs), 1)
        self.assertEqual(output.read_bytes(), b'keep output')
        failures = [item['failure']['code'] for item in self.service.batch_task['items'] if item['status'] == 'failed']
        self.assertEqual(set(failures), {'error.source_access', 'error.output_exists'})
        with self.assertLogs('borasuki.service', level='ERROR'):
            self.start(paths).result(5)
        self.assertEqual(len(self.service.jobs), 1)
        self.assertIn('error.output_reserved', [item['failure']['code'] for item in self.service.batch_task['items']])

    def test_recipe_gpu_and_preset_are_frozen_at_submission(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.service._prepare
        def prepare(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)
        values = self.recipe()
        preset = self.service.save_preset('Batch preset', values)
        expected = copy.deepcopy(values)
        with patch.object(self.service, '_prepare', side_effect=prepare):
            future = self.start(recipe=values, preset=preset['id'])
            self.assertTrue(entered.wait(3))
            values['custom']['brightness'] = 0.1
            self.service.save_settings({'default_gpu': 0})
            self.service.delete_preset(preset['id'], True)
            with self.assertRaisesRegex(ValueError, 'error.setup_busy'):
                self.service.request_setup()
            release.set()
            future.result(5)
        for job in self.service.jobs.values():
            self.assertEqual(job['configuration']['preset'], preset)
            self.assertEqual(job['grade'], expected['custom'])
            self.assertEqual(job['gpu_id'], 1)

    def test_adaptive_analyzes_each_source_and_shares_only_overrides(self):
        baseline = [{'contrast': 1.01, 'brightness': 0, 'saturation': 1.009},
                    {'contrast': 1.025, 'brightness': -0.01, 'saturation': 0.995}]
        with patch.object(self.service, '_analyze_config', side_effect=baseline) as analyze:
            self.start(recipe=self.recipe('adaptive')).result(5)
        self.assertEqual(analyze.call_count, 2)
        self.assertEqual(len({call.args[0]['source'] for call in analyze.call_args_list}), 2)
        for job, result in zip(self.service.jobs.values(), baseline):
            self.assertEqual(job['analysis'], result)
            self.assertEqual(job['grade'], {**result, 'brightness': 0.01})

    def test_cancel_stops_remaining_preparation_not_already_added_jobs(self):
        entered = threading.Event()
        original = self.service._prepare
        calls = 0
        def prepare(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                self.assertTrue(kwargs['stop'].wait(5))
                raise Interrupted()
            return original(*args, **kwargs)
        with patch.object(self.service, '_prepare', side_effect=prepare):
            future = self.start()
            self.assertTrue(entered.wait(3))
            with self.assertRaisesRegex(ValueError, 'error.batch_busy'):
                self.start()
            self.service.cancel_batch(self.service.batch_task['id'])
            future.result(5)
        self.assertEqual(self.service.batch_task['status'], 'cancelled')
        self.assertEqual([item['status'] for item in self.service.batch_task['items']], ['added', 'not_added'])
        self.assertEqual([job['status'] for job in self.service.jobs.values()], ['queued'])
        self.assertFalse(self.service.stop.is_set())

    def test_shutdown_cancels_queued_preparation_without_late_commit(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocking_analysis():
            entered.set()
            release.wait(5)
        occupied = self.service.analysis_pool.submit(blocking_analysis)
        self.assertTrue(entered.wait(3))
        future = self.start()
        self.service.begin_shutdown(True)
        self.assertTrue(future.cancelled())
        self.assertEqual(self.service.batch_task['status'], 'cancelled')
        release.set()
        occupied.result(3)
        self.service.shutdown()
        self.assertFalse(self.service.jobs)

    def test_bridge_picker_and_drop_only_select_sources(self):
        api = API(self.service)
        api._window = Mock()
        paths = self.sources()
        api._window.create_file_dialog.return_value = paths
        result = api.choose_batch()
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['paths'], sorted(paths, key=str.casefold))
        self.assertTrue(api._window.create_file_dialog.call_args.kwargs['allow_multiple'])
        api._window.create_file_dialog.return_value = None
        self.assertIsNone(api.choose_batch()['data'])
        api._window.evaluate_js.return_value = True
        api._drop({'dataTransfer': {'files': [{'pywebviewFullPath': path} for path in paths]}})
        self.assertIn('window.finishFileImport({"batch":', api._window.evaluate_js.call_args.args[0])
        self.assertFalse(self.service.jobs)
        with patch('borasuki.app.collect_videos', side_effect=ValueError('error.batch_limit')), self.assertLogs('borasuki.app', level='ERROR'):
            api._drop({'dataTransfer': {'files': [{'pywebviewFullPath': path} for path in paths]}})
        self.assertIn('window.finishFileImport({"failure":', api._window.evaluate_js.call_args.args[0])

    def test_setup_and_invalid_recipe_gpu_and_folder_are_rejected(self):
        paths = self.sources()
        for folder, values, gpu in [('', self.recipe(), 1), (str(self.root), {}, 1), (str(self.root), self.recipe(), 5)]:
            with self.assertRaises(ValueError):
                self.service.request_batch(paths, folder, values, gpu)
        self.service.setup['status'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'error.setup_required'):
            self.start()
        self.assertFalse(self.service.jobs)

    def test_batch_does_not_mutate_running_job(self):
        identifier = test_queue.QueueTests.create(self)
        self.service.active_id = identifier
        self.service.update(identifier, status='running')
        before = copy.deepcopy(self.service.jobs[identifier])
        self.start().result(5)
        self.assertEqual(self.service.jobs[identifier], before)
        self.assertEqual(self.service.active_id, identifier)
        self.assertFalse(self.service.stop.is_set())
        self.service.active_id = None

    def test_storage_failure_is_per_file_and_never_reports_added(self):
        save = self.service.store.save
        calls = 0
        def write(job):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError('disk full')
            return save(job)
        with patch.object(self.service.store, 'save', side_effect=write), self.assertLogs('borasuki.service', level='ERROR'):
            self.start().result(5)
        self.assertEqual([item['status'] for item in self.service.batch_task['items']], ['failed', 'added'])
        self.assertEqual(self.service.batch_task['items'][0]['failure']['code'], 'error.disk_space')
        self.assertEqual(len(self.reload().jobs), 1)

    def test_shutdown_waits_for_running_batch_and_rejects_late_prepare(self):
        entered = threading.Event()
        original = self.service._prepare
        def prepare(*args, **kwargs):
            job = original(*args, **kwargs)
            entered.set()
            self.assertTrue(kwargs['stop'].wait(5))
            return job
        with patch.object(self.service, '_prepare', side_effect=prepare):
            future = self.start()
            self.assertTrue(entered.wait(3))
            self.service.shutdown()
            self.assertTrue(future.done())
        self.assertEqual(self.service.batch_task['status'], 'cancelled')
        self.assertFalse(self.service.jobs)
