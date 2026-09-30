"""Preparation cache identity, stale assets, GPU ownership and queue gates."""

import copy
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_queue
from borasuki import preparation
from borasuki.errors import failure_info
from borasuki.process import Interrupted
from borasuki.storage import atomic_json

REQUIRE = preparation.require


class ReceiptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for frontend contract')
    def test_frontend_debounce_stale_result_and_retry(self):
        subprocess.run([shutil.which('node'), str(Path(__file__).with_name('preparation_frontend.cjs'))],
                       check=True, timeout=15, capture_output=True)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = {'runtime': {}, 'cache': str(self.root / 'cache'), 'gpu_id': 0,
                       'tile': [486, 276], 'media': {'width': 1920, 'height': 1080},
                       'upscale': {'scale': 2}, 'denoise': {'enabled': True, 'strength': 3}}
        self.signature = patch('borasuki.preparation.validation_signature', return_value={'runtime': 1})
        self.signature.start()
        self.addCleanup(self.signature.stop)
        self.engine = self.root / 'cache/test.engine'
        self.engine.parent.mkdir()
        self.engine.write_bytes(b'e' * 2048)

    def receipt(self):
        stat = self.engine.stat()
        atomic_json(preparation.receipt_path(self.config), {'signature': preparation.identity(self.config)[1],
                     'engines': [[str(self.engine), stat.st_size, stat.st_mtime_ns]]})

    def test_source_color_output_do_not_rebuild_but_processing_changes_do(self):
        self.receipt()
        other = {**self.config, 'source': 'another.mp4', 'output': 'another.mkv', 'grade': {'contrast': 1.1}}
        self.assertTrue(preparation.ready(other))
        for change in ({'gpu_id': 1}, {'tile': [240, 136]}, {'denoise': {'enabled': True, 'strength': 1}},
                       {'trt_streams': 2}, {'media': {'width': 1280, 'height': 720}}):
            self.assertFalse(preparation.ready({**self.config, **change}))
        with patch('borasuki.preparation.validation_signature', return_value={'runtime': 2}):
            self.assertFalse(preparation.ready(self.config))

    def test_missing_modified_or_external_engine_is_not_ready(self):
        self.assertFalse(preparation.ready(self.config))
        self.receipt()
        self.assertTrue(preparation.ready(self.config))
        self.engine.write_bytes(b'x')
        self.assertFalse(preparation.ready(self.config))
        self.engine = self.root / 'external.engine'
        self.engine.write_bytes(b'e' * 2048)
        self.receipt()
        self.assertFalse(preparation.ready(self.config))
        with self.assertRaisesRegex(ValueError, 'engine_preparation_required'):
            REQUIRE(self.config)

    def test_cached_preparation_runs_no_process_and_cancelled_preparation_has_no_receipt(self):
        self.receipt()
        with patch('borasuki.preparation.ProcessGroup') as group:
            preparation.prepare(self.config, self.root, threading.Event())
        group.assert_not_called()
        preparation.receipt_path(self.config).unlink()
        self.config['runtime']['vspipe'] = 'fixture'
        with patch('borasuki.preparation.check_hardware_encoder'), \
             patch('borasuki.preparation.ProcessGroup.capture', side_effect=Interrupted()):
            with self.assertRaises(Interrupted):
                preparation.prepare(self.config, self.root, threading.Event())
        self.assertFalse(preparation.ready(self.config))


class PreparationServiceTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp

    def request(self):
        return self.service.request_preparation(str(self.source), {'scale': 2}, {'enabled': True, 'strength': 1}, 0)

    def test_missing_preparation_blocks_queue_job_without_commit(self):
        with patch('borasuki.preparation.require', side_effect=ValueError('error.engine_preparation_required')) as require:
            with self.assertRaisesRegex(ValueError, 'engine_preparation_required'):
                test_queue.QueueTests.create(self)
        require.assert_called_once()
        self.assertFalse(self.service.jobs)

    def test_adaptive_job_requires_preparation_without_interrupting_active_render(self):
        active = test_queue.QueueTests.create(self, 'active')
        self.service.active_id = active
        self.service.update(active, status='running')
        grade = {'contrast': 1.02, 'brightness': 0.01, 'saturation': 1.01}
        with patch.object(self.service, '_analyze_config', return_value=grade):
            token = self.service.request_analysis(str(self.source))
            self.service.analysis_task['future'].result(timeout=3)
        with patch('borasuki.preparation.require', side_effect=ValueError('error.engine_preparation_required')) as require:
            with self.assertRaisesRegex(ValueError, 'engine_preparation_required'):
                self.service.create(str(self.source), str(self.root / 'adaptive.mkv'), 'adaptive', 0, {},
                    adaptive={'token':token, 'profile':'normal', 'overrides':{}})
        require.assert_called_once()
        self.assertEqual(self.service.jobs[active]['status'], 'running')
        self.assertFalse(self.service.stop.is_set())
        self.assertEqual(list(self.service.jobs), [active])
        second = self.service.create(str(self.source), str(self.root / 'adaptive.mkv'), 'adaptive', 0, {},
            adaptive={'token':token, 'profile':'normal', 'overrides':{}})
        self.assertEqual(self.service.jobs[second]['configuration']['analysis'], grade)

    def test_missing_preparation_blocks_preview_draft_edit_and_batch(self):
        job = test_queue.QueueTests.create(self)
        edited = self.service.begin_edit(job)
        values = test_queue.QueueTests.preview_values(self)
        with patch('borasuki.preparation.require', side_effect=ValueError('error.engine_preparation_required')):
            for command in (lambda: self.service.check_draft(values),
                            lambda: self.service.request_preview(values),
                            lambda: self.service.request_preview({}, editing={'id':job, 'token':edited['edit_token']}, saved=True),
                            lambda: self.service.end_edit(job, edited['edit_token'], values)):
                with self.assertRaisesRegex(ValueError, 'engine_preparation_required'):
                    command()
            token = self.service.request_batch([str(self.source)], str(self.root),
                {'upscale':{'scale':2}, 'denoise':{'enabled':True,'strength':1}, 'color_mode':'original'}, 0)
            self.service.batch_task['future'].result(timeout=3)
        self.assertIsNone(self.service.preview_task)
        self.assertEqual(self.service.jobs[job]['status'], 'editing')
        self.assertEqual(list(self.service.jobs), [job])
        self.assertEqual(self.service.batch_task['items'][0]['failure']['code'], 'error.engine_preparation_required')

    def test_internal_exception_does_not_blame_gpu(self):
        for exc in (KeyError('range'), RuntimeError("Script evaluation failed:\nKeyError: 'range'")):
            self.assertEqual(failure_info(exc, 'error.engine_preparation_failed')['code'], 'error.internal')
        self.assertEqual(failure_info(RuntimeError('CUDA out of memory'))['code'], 'error.gpu_memory')

    def test_retry_resume_and_preview_commit_reject_cache_loss_without_mutation(self):
        identifier = test_queue.QueueTests.create(self)
        for status, action in (('paused', 'resume'), ('failed', 'retry')):
            self.service.update(identifier, status=status)
            before = copy.deepcopy(self.service.jobs[identifier])
            with patch('borasuki.preparation.require', side_effect=ValueError('error.engine_preparation_required')):
                with self.assertRaisesRegex(ValueError, 'engine_preparation_required'):
                    self.service.action(identifier, action)
            self.assertEqual(self.service.jobs[identifier], before)
        self.service.preview_task = {'id':'preview', 'status':'ready', 'job':before, 'editing':None, 'stop':threading.Event()}
        with patch('borasuki.preparation.require', side_effect=ValueError('error.engine_preparation_required')):
            with self.assertRaisesRegex(ValueError, 'engine_preparation_required'):
                self.service.commit_preview('preview')
        self.assertEqual(self.service.preview_task['status'], 'ready')
        self.assertEqual(self.service.jobs[identifier], before)

    def test_queue_snapshot_ignores_vram_consumed_by_active_render_for_tile_choice(self):
        self.media.update(width=1920, height=1080)
        runtime = copy.deepcopy(self.runtime)
        runtime['gpus'][0]['free'] = 800
        self.service.active_id = 'existing-render'
        with patch('borasuki.service.discover', return_value=runtime):
            identifier = test_queue.QueueTests.create(self, 'next-video')
        self.service.active_id = None
        self.assertEqual(self.service.jobs[identifier]['configuration']['tile'], [486, 276])

    def test_preparation_is_not_a_queue_job_and_deduplicates_selected_settings(self):
        with patch('borasuki.preparation.identity', return_value=('key', {})), patch('borasuki.preparation.ready', return_value=False):
            token = self.request()
            self.assertEqual(self.request(), token)
            view = self.service.snapshot()['preparation']
        self.assertFalse(self.service.jobs)
        self.assertEqual(view['status'], 'waiting')
        self.assertNotIn('config', view)
        self.service.cancel_preparation(token)
        self.assertTrue(self.service.engine_task['stop'].is_set())
        self.assertEqual(self.service.engine_task['status'], 'cancelled')

    def test_active_render_is_never_interrupted_by_preparation(self):
        identifier = test_queue.QueueTests.create(self)
        entered, release, prepared = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def render(job, stop, update):
            entered.set()
            if not release.wait(4):
                raise AssertionError('Test render not released')
            self.assertFalse(stop.is_set())
            update(status='completed')
        def prepare(config, data, stop, update=None):
            self.assertIsNone(self.service.active_id)
            prepared.set()
        with patch('borasuki.service.execute', side_effect=render), \
             patch('borasuki.preparation.identity', return_value=('key', {})), \
             patch('borasuki.preparation.ready', side_effect=lambda config: 'id' in config), \
             patch('borasuki.preparation.prepare', side_effect=prepare):
            self.service.worker.start()
            self.service.wake.set()
            self.assertTrue(entered.wait(3))
            self.request()
            self.assertFalse(prepared.is_set())
            self.assertEqual(self.service.active_id, identifier)
            release.set()
            self.assertTrue(prepared.wait(3))
            self.service.shutdown()

    def test_queued_job_prepares_serially_before_preflight_and_render(self):
        identifier = test_queue.QueueTests.create(self)
        order = []
        done = threading.Event()

        def prepare(config, data, stop, update=None):
            order.append('prepare')
            self.assertEqual(self.service.active_id, identifier)
            current = self.service.snapshot()['jobs'][0]
            self.assertEqual(current['status'], 'running')
            self.assertEqual(current['stage'], 'engine_preparing')
            self.assertNotIn('pause', current['actions'])
            self.assertIn('cancel', current['actions'])
            with self.assertRaisesRegex(ValueError, 'error.invalid_action'):
                self.service.action(identifier, 'pause')

        def check(job, runtime, **kwargs):
            order.append('preflight')
            self.assertEqual(self.service.jobs[identifier]['stage'], 'preflight')
            self.assertTrue(job['require_engine_prepared'])
            return {'warning': None, 'volumes': []}

        def render(job, stop, update):
            order.append('render')
            self.assertTrue(job['require_engine_prepared'])
            update(status='completed')
            done.set()

        with patch('borasuki.service.discover', return_value=self.runtime), \
             patch('borasuki.preparation.ready', side_effect=[False, True]), \
             patch('borasuki.preparation.prepare', side_effect=prepare), \
             patch('borasuki.service.preflight', side_effect=check), \
             patch('borasuki.service.execute', side_effect=render):
            self.service.worker.start()
            self.service.wake.set()
            self.assertTrue(done.wait(3))
            self.service.shutdown()

        self.assertEqual(order, ['prepare', 'preflight', 'render'])
        self.assertEqual(self.service.jobs[identifier]['status'], 'completed')

    def test_cancelled_inflight_request_can_be_replaced_and_cache_loss_is_visible(self):
        with patch('borasuki.preparation.identity', return_value=('key', {})), patch('borasuki.preparation.ready', return_value=False):
            token = self.request()
            old = self.service.engine_task
            old['status'] = 'preparing'
            self.service.cancel_preparation(token)
            self.assertNotEqual(self.request(), token)
            self.assertTrue(old['stop'].is_set())
            self.service.engine_task['status'] = 'ready'
            view = self.service.snapshot()['preparation']
            self.assertEqual(view['status'], 'failed')
            self.assertEqual(view['failure']['code'], 'error.engine_preparation_required')

    def test_guard_error_preserves_cause_and_safe_recovery(self):
        failure = failure_info(RuntimeError('VSPipe: error.engine_preparation_required\nbroken pipe'))
        self.assertEqual(failure['recovery'], 'preparation')
        self.assertFalse(failure['retryable'])
        script = (preparation.ROOT / 'borasuki/render.vpy').read_text()
        self.assertLess(script.index("raise RuntimeError('error.engine_preparation_required')"), script.index('result = subprocess.run'))


if __name__ == '__main__':
    unittest.main()
