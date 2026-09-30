"""Storage accounting and cleanup must not touch sources or active jobs."""

import unittest
import copy
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import test_queue
from borasuki.storage import atomic_json, data_directory


class DataDirectoryTests(unittest.TestCase):
    def test_new_install_uses_empty_local_app_data(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {'LOCALAPPDATA': root}, clear=True):
                path = data_directory()
            self.assertEqual(path, Path(root).resolve() / 'KaraKedi' / 'Borasuki')
            self.assertFalse(path.exists())

    def test_explicit_data_directory_wins(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {'LOCALAPPDATA': root, 'BORASUKI_DATA_DIR': str(Path(root) / 'isolated')}, clear=True):
                self.assertEqual(data_directory(), Path(root).resolve() / 'isolated')


class StorageUsageTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp
    create = test_queue.QueueTests.create

    def test_not_yet_created_or_already_cleaned_work_is_zero_not_scan_failure(self):
        identifier = self.create()
        summary = self.service.storage_usage()
        self.assertEqual(summary['jobs'][0]['bytes'], 0)
        self.assertFalse(summary['incomplete'])
        self.service.update(identifier, status='cancelled')
        self.service.cleanup_job_files(identifier, True)
        self.assertFalse(self.service.storage_usage()['incomplete'])

    def files(self, identifier):
        work = self.service.data / 'jobs' / identifier
        for revision in ('revision-0', 'revision-1'):
            (work / revision).mkdir(parents=True, exist_ok=True)
            (work / revision / 'segment.mkv').write_bytes(b'frame')
        return work

    def test_accounting_cleans_all_revisions_but_keeps_history_and_video(self):
        identifier = self.create()
        work = self.files(identifier)
        output = Path(self.service.jobs[identifier]['output'])
        output.write_bytes(b'completed video')
        self.service.update(identifier, status='completed')
        summary = self.service.storage_usage()
        self.assertEqual(summary['bytes'], 10)
        self.assertTrue(summary['jobs'][0]['cleanable'])
        with self.assertRaisesRegex(ValueError, 'error.confirmation'):
            self.service.cleanup_job_files(identifier)
        self.assertTrue(work.exists())
        self.service.cleanup_job_files(identifier, True)
        self.assertFalse(work.exists())
        self.assertEqual(output.read_bytes(), b'completed video')
        self.assertEqual(self.source.read_bytes(), b'source')
        self.assertEqual(self.service.jobs[identifier]['status'], 'completed')
        self.assertEqual(self.service.storage_usage()['bytes'], 0)

    def test_active_and_queued_are_not_cleanable(self):
        identifier = self.create()
        work = self.files(identifier)
        for status in ('queued', 'running', 'pause_requested', 'cancel_requested', 'editing'):
            self.service.update(identifier, status=status)
            self.assertFalse(self.service.storage_usage()['jobs'][0]['cleanable'])
            with self.assertRaisesRegex(ValueError, 'error.job_locked'):
                self.service.cleanup_job_files(identifier, True)
            self.assertTrue(work.exists())

    def test_partial_failure_blocks_resume_and_can_be_cleaned_again(self):
        identifier = self.create()
        self.files(identifier)
        self.service.update(identifier, status='paused')
        with patch('borasuki.service.shutil.rmtree', side_effect=PermissionError('busy')):
            with self.assertRaises(PermissionError):
                self.service.cleanup_job_files(identifier, True)
        with self.assertRaisesRegex(ValueError, 'error.resume_deleted'):
            self.service.action(identifier, 'resume')
        self.assertTrue(self.service.storage_usage()['jobs'][0]['cleanable'])
        self.service.cleanup_job_files(identifier, True)

    def test_source_inside_old_revision_is_never_deleted(self):
        identifier = self.create()
        work = self.files(identifier)
        self.service.jobs[identifier].update(status='cancelled', source=str(work / 'revision-1/segment.mkv'))
        summary = self.service.storage_usage()
        self.assertTrue(summary['incomplete'])
        self.assertIsNone(summary['jobs'][0]['bytes'])
        with self.assertRaisesRegex(ValueError, 'error.unsafe_cleanup'):
            self.service.cleanup_job_files(identifier, True)
        self.assertTrue(work.exists())

    def test_other_jobs_source_or_open_draft_is_protected(self):
        first = self.create()
        work = self.files(first)
        self.service.update(first, status='completed')
        second = self.create('second')
        self.service.jobs[second]['source'] = str(work / 'revision-1/segment.mkv')
        with self.assertRaisesRegex(ValueError, 'error.unsafe_cleanup'):
            self.service.cleanup_job_files(first, True)
        del self.service.jobs[second]
        self.service.inspect(str(work / 'revision-1/segment.mkv'))
        with self.assertRaisesRegex(ValueError, 'error.unsafe_cleanup'):
            self.service.cleanup_job_files(first, True)
        self.assertTrue(work.exists())

    def orphan(self):
        identifier = self.create()
        job = copy.deepcopy(self.service.jobs[identifier])
        work = Path(job['work'])
        work.mkdir(parents=True, exist_ok=True)
        atomic_json(work / 'config.json', job)
        (work / 'segment-0000000000-0000000004.mkv').write_bytes(b'old segment')
        self.service.remove('queue', identifier, True)
        return job, work.parent

    def test_orphan_cleanup_requires_config_evidence_and_keeps_source(self):
        job, root = self.orphan()
        rows = self.service.storage_usage()['extras']
        row = next(row for row in rows if row['kind'] == 'orphan')
        self.assertTrue(row['cleanable'])
        self.assertGreater(row['bytes'], 0)
        with self.assertRaisesRegex(ValueError, 'error.confirmation'):
            self.service.cleanup_extra_files(row['id'])
        self.service.cleanup_extra_files(row['id'], True)
        self.assertFalse(root.exists())
        self.assertTrue(self.source.exists())
        self.assertNotIn(job['id'], self.service.jobs)

    def test_unknown_files_or_forged_orphan_config_are_not_deleted(self):
        job, root = self.orphan()
        (root / 'personal.txt').write_text('keep')
        row = next(row for row in self.service.storage_usage()['extras'] if row['kind'] == 'orphan')
        self.assertFalse(row['cleanable'])
        with self.assertRaisesRegex(ValueError, 'error.unsafe_cleanup'):
            self.service.cleanup_extra_files(row['id'], True)
        (root / 'personal.txt').unlink()
        atomic_json(Path(job['work']) / 'config.json', {**job, 'id':'a' * 32})
        with self.assertRaisesRegex(ValueError, 'error.unsafe_cleanup'):
            self.service.cleanup_extra_files(row['id'], True)
        self.assertTrue(root.exists())

    def test_cache_deletes_only_recognized_files_and_preserves_job_state(self):
        identifier = self.create()
        before = copy.deepcopy(self.service.jobs[identifier])
        cache = self.service.data / 'engine-cache'
        cache.mkdir(exist_ok=True)
        (cache / '12345678.engine').write_bytes(b'engine')
        (cache / '12345678.engine.cache').write_bytes(b'cache')
        (cache / 'personal.txt').write_text('keep')
        row = next(row for row in self.service.storage_usage()['extras'] if row['kind'] == 'cache')
        self.assertEqual(row['bytes'], 15)
        self.service.cleanup_extra_files('cache', True)
        self.assertEqual([path.name for path in cache.iterdir()], ['personal.txt'])
        self.assertEqual(self.service.jobs[identifier], before)

    def test_cache_cannot_delete_a_source_or_run_during_processing(self):
        identifier = self.create()
        cache = self.service.data / 'engine-cache'
        cache.mkdir(exist_ok=True)
        engine = cache / '12345678.engine'
        engine.write_bytes(b'engine')
        self.service.update(identifier, status='running')
        with self.assertRaisesRegex(ValueError, 'error.temp_busy'):
            self.service.cleanup_extra_files('cache', True)
        self.service.update(identifier, status='paused')
        self.service.jobs[identifier]['source'] = str(engine)
        with self.assertRaisesRegex(ValueError, 'error.unsafe_cleanup'):
            self.service.cleanup_extra_files('cache', True)
        self.assertTrue(engine.exists())

    def test_partial_needs_embedded_owner_and_is_removed_before_work(self):
        identifier = self.create()
        root = self.files(identifier)
        self.service.update(identifier, status='failed')
        partial = self.service._partial_path(self.service.jobs[identifier])
        partial.write_bytes(b'partial')
        row = next(row for row in self.service.storage_usage()['extras'] if row['kind'] == 'partial')
        with self.assertRaisesRegex(ValueError, 'error.temp_partial_first'):
            self.service.cleanup_job_files(identifier, True)
        with patch('borasuki.pipeline.probe', return_value={'format':{'tags':{}}}):
            with self.assertRaisesRegex(ValueError, 'error.temp_owner'):
                self.service.cleanup_extra_files(row['id'], True)
        self.assertTrue(partial.exists())
        with patch('borasuki.pipeline.probe', return_value={'format':{'tags':{'BORASUKI_JOB':identifier}}}):
            self.service.cleanup_extra_files(row['id'], True)
        self.assertFalse(partial.exists())
        self.service.cleanup_job_files(identifier, True)
        self.assertFalse(root.exists())
        self.assertTrue(self.source.exists())

    def test_partial_replaced_during_probe_is_not_deleted(self):
        identifier = self.create()
        self.service.update(identifier, status='failed')
        partial = self.service._partial_path(self.service.jobs[identifier])
        partial.write_bytes(b'partial')
        row = next(row for row in self.service.storage_usage()['extras'] if row['kind'] == 'partial')
        def replaced(*args):
            partial.write_bytes(b'replaced by another program')
            return {'format':{'tags':{'borasuki_job':identifier}}}
        with patch('borasuki.pipeline.probe', side_effect=replaced):
            with self.assertRaisesRegex(ValueError, 'error.unsafe_cleanup'):
                self.service.cleanup_extra_files(row['id'], True)
        self.assertEqual(partial.read_bytes(), b'replaced by another program')

    def test_removed_job_partial_is_still_discoverable(self):
        job, root = self.orphan()
        partial = self.service._partial_path(job)
        partial.write_bytes(b'partial')
        rows = self.service.storage_usage()['extras']
        row = next(row for row in rows if row['kind'] == 'partial')
        self.assertTrue(row['cleanable'])
        with self.assertRaisesRegex(ValueError, 'error.temp_partial_first'):
            self.service.cleanup_extra_files('orphan:' + job['id'], True)
        with patch('borasuki.pipeline.probe', return_value={'format':{'tags':{'borasuki_job':job['id']}}}):
            self.service.cleanup_extra_files(row['id'], True)
        self.service.cleanup_extra_files('orphan:' + job['id'], True)
        self.assertFalse(root.exists())

    def test_ready_preview_is_protected_but_abandoned_preview_is_cleanable(self):
        job, root = self.orphan()
        (Path(job['work']) / 'preview-original.mp4').write_bytes(b'preview')
        self.service.preview_task = {'id':job['id'], 'status':'ready', 'values':{}}
        row = next(row for row in self.service.storage_usage()['extras'] if row['kind'] == 'orphan')
        self.assertFalse(row['cleanable'])
        with self.assertRaisesRegex(ValueError, 'error.unsafe_cleanup'):
            self.service.cleanup_extra_files(row['id'], True)
        self.service.preview_task = None
        self.service.cleanup_extra_files(row['id'], True)
        self.assertFalse(root.exists())
