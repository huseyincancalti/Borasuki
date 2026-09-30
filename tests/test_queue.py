"""Queue acceptance rules without GPU inference or user data."""

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from borasuki.preflight import check, RESERVE
from borasuki.process import Interrupted
from borasuki.profile import cugan_model_name, cugan_options, processing_settings
from borasuki.service import Service
from borasuki.storage import atomic_json


class QueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"source")
        plugins = self.root / "plugins"
        for path in (plugins / "vsmlrt.py", plugins / "vstrt.dll", plugins / "models/cugan/up2x-latest-denoise1x.onnx", self.root / "vspipe.exe"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test runtime")
        for noise in (-1, 2, 3):
            (plugins / "models/cugan" / cugan_model_name(noise)).write_bytes(f"model {noise}".encode())
        self.runtime = {"ready": True, "missing": [], "plugins": str(plugins), "vspipe": str(self.root / "vspipe.exe"),
                        "gpus": [{"id": 0, "name": "Test NVIDIA", "driver": "test", "free": 4000, "total": 4000},
                                 {"id": 1, "name": "Second NVIDIA", "driver": "test", "free": 2000, "total": 4000}]}
        self.media = {"width": 480, "height": 270, "fps": 24, "duration": 10, "matrix": 1, "range": 1,
                      "assumed_color": False, "streams": [], "video_index": 0}
        for target, value in (("borasuki.service.discover", self.runtime), ("borasuki.service.inspect_source", self.media),
                              ("borasuki.service.seed_cache", None), ("borasuki.service.gpu_memory", {}),
                              ("borasuki.preparation.require", None),
                              ("borasuki.preparation.ready", True),
                              ("borasuki.service.validation_signature", {"fixture": True}),
                              ("borasuki.service.Service._load_setup", {"status": "ready", "signature": {"fixture": True}})):
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.service = Service(self.root / "app", start_worker=False)
        self.addCleanup(self.service.shutdown)
        self.service.save_settings({"notifications": False})

    def create(self, name="output", mode="reference"):
        return self.service.create(str(self.source), str(self.root / (name + ".mkv")), mode, 0,
                                   {"contrast": 1.02, "brightness": 0.01, "saturation": 1.01})

    def reload(self):
        reloaded = Service(self.service.data, start_worker=False)
        self.addCleanup(reloaded.shutdown)
        return reloaded

    def test_legacy_failed_job_recovers_cause_without_retrying(self):
        identifier = self.create()
        work = Path(self.service.jobs[identifier]['work'])
        work.mkdir(parents=True, exist_ok=True)
        cause = 'Error: fwrite() call failed when writing video plane 0, errno: 22, frame: 38'
        (work / 'render.log').write_text(cause + '\n' + 'frame=38\n' * 100)
        self.service.update(identifier, status='failed', error='frame=38')
        job = self.reload().jobs[identifier]
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['failure']['code'], 'error.frame_transfer')
        self.assertIn(cause, job['failure']['detail'])

    def pause_running(self, identifier):
        self.service.active_id = identifier
        self.service.stop.clear()
        self.service.update(identifier, status="running")
        self.service.action(identifier, "pause")
        self.assertEqual(self.service.jobs[identifier]["status"], "pause_requested")
        self.assertTrue(self.service.stop.is_set())
        # Worker acknowledgement for retention fixtures.
        self.service.update(identifier, status="paused", paused_at=time.time())
        self.service.active_id = None

    def test_queued_rejects_runtime_actions_without_mutation(self):
        identifier = self.create()
        before = copy.deepcopy(self.service.jobs[identifier])
        for active_id in (None, identifier):
            self.service.active_id = active_id
            for action in ("pause", "resume", "cancel"):
                with self.subTest(active_id=active_id, action=action):
                    with self.assertRaisesRegex(ValueError, "error.invalid_action"):
                        self.service.action(identifier, action, confirmed=True)
                    self.assertEqual(self.service.jobs[identifier], before)
                    self.assertFalse(self.service.stop.is_set())
        self.assertEqual(self.reload().jobs[identifier]["status"], "queued")

    def test_pause_requires_running_worker_and_resume_requires_paused(self):
        identifier = self.create()
        for status in ("editing", "running", "pause_requested", "cancel_requested", "completed", "failed", "cancelled"):
            self.service.update(identifier, status=status)
            for action in ("pause", "resume"):
                with self.subTest(status=status, action=action):
                    with self.assertRaisesRegex(ValueError, "error.invalid_action"):
                        self.service.action(identifier, action)
                    self.assertEqual(self.service.jobs[identifier]["status"], status)
        self.service.active_id = identifier
        for status in ("pause_requested", "cancel_requested", "paused"):
            self.service.update(identifier, status=status)
            for action in ("pause", "resume"):
                with self.subTest(active_status=status, action=action):
                    with self.assertRaisesRegex(ValueError, "error.invalid_action"):
                        self.service.action(identifier, action)

    def test_worker_acknowledges_pause_before_resume(self):
        identifier = self.create()
        started, stopping = threading.Event(), threading.Event()

        def runner(job, stop, update):
            started.set()
            if not stop.wait(3):
                raise RuntimeError("Pause signal not received")
            stopping.set()
            raise Interrupted()

        with patch("borasuki.service.execute", side_effect=runner):
            self.service.worker.start()
            self.assertTrue(started.wait(3))
            self.service.save_settings({"queue_enabled": False})
            self.service.action(identifier, "pause")
            self.assertTrue(stopping.wait(3))
            deadline = time.monotonic() + 3
            while self.service.active_id is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNone(self.service.active_id)
            self.assertEqual(self.service.jobs[identifier]["status"], "paused")
            self.assertEqual(self.reload().jobs[identifier]["status"], "paused")
            self.service.action(identifier, "resume")
            self.assertEqual(self.service.jobs[identifier]["status"], "queued")
            self.assertEqual(self.reload().jobs[identifier]["status"], "queued")
            self.service.shutdown()

    def test_cancel_requires_confirmation_and_is_terminal(self):
        identifier = self.create()
        self.pause_running(identifier)
        with self.assertRaisesRegex(ValueError, "error.confirmation"):
            self.service.action(identifier, "cancel")
        self.service.action(identifier, "cancel", confirmed=True)
        for action in ("retry", "resume", "pause"):
            with self.assertRaisesRegex(ValueError, "error.invalid_action"):
                self.service.action(identifier, action)
        self.assertEqual(self.reload().jobs[identifier]["status"], "cancelled")

    def test_pause_resume_and_failed_retry_are_distinct(self):
        identifier = self.create()
        self.pause_running(identifier)
        self.assertEqual(self.service.jobs[identifier]["status"], "paused")
        self.service.action(identifier, "resume")
        self.service.update(identifier, status="failed")
        with self.assertRaises(ValueError):
            self.service.action(identifier, "resume")
        self.service.action(identifier, "retry")
        self.assertEqual(self.service.jobs[identifier]["status"], "queued")

    def test_active_selection_is_locked_before_running_status(self):
        identifier = self.create()
        self.service.active_id = identifier
        self.assertTrue(self.service.snapshot()["jobs"][0]["locked"])
        for operation in (lambda: self.service.begin_edit(identifier),
                          lambda: self.service.remove("queue", identifier, True),
                          lambda: self.service.reorder([identifier])):
            with self.assertRaises(ValueError):
                operation()
        with self.assertRaisesRegex(ValueError, "error.invalid_action"):
            self.service.action(identifier, "pause")
        self.assertFalse(self.service.stop.is_set())
        self.service.update(identifier, status="running", stage="preparing")
        self.service.action(identifier, "pause")
        self.service.update(identifier, status="running", stage="preparing")
        self.assertTrue(self.service.stop.is_set())
        self.assertEqual(self.service.jobs[identifier]["status"], "pause_requested")

    def test_cancel_intent_is_not_lost_after_crash(self):
        identifier = self.create()
        self.service.update(identifier, status="cancel_requested")
        self.assertEqual(self.reload().jobs[identifier]["status"], "cancelled")
        self.service.update(identifier, status="pause_requested")
        self.assertEqual(self.reload().jobs[identifier]["status"], "paused")

    def test_snapshot_is_independent_of_settings_and_callers(self):
        identifier = self.create(mode="custom")
        before = copy.deepcopy(self.service.jobs[identifier]["configuration"])
        self.service.save_settings({"default_gpu": 1, "output_folder": "different"})
        exposed = self.service.snapshot()
        exposed["jobs"][0]["configuration"]["grade"]["contrast"] = 99
        self.assertEqual(self.service.jobs[identifier]["configuration"], before)
        self.assertEqual(self.reload().jobs[identifier]["configuration"], before)
        self.assertEqual(len(before["model"]["version"]), 64)
        with self.assertRaisesRegex(ValueError, "error.job_locked"):
            self.service.update(identifier, grade={})

    def test_adaptive_grade_is_resolved_before_enqueue(self):
        grade = {"contrast": 1.025, "brightness": 0.0, "saturation": 1.01, "reason": "enhanced"}
        with self.assertRaisesRegex(ValueError, "error.analysis_stale"):
            self.create(mode="adaptive")
        with patch.object(self.service, "_analyze_config", return_value=grade):
            token = self.service.request_analysis(str(self.source))
            self.service.analysis_task["future"].result(timeout=3)
        identifier = self.service.create(str(self.source), str(self.root / "adaptive.mkv"), "adaptive", 0, {},
            {"scale": 2}, {"enabled": False, "strength": 3}, {"token": token})
        config = self.service.jobs[identifier]["configuration"]
        self.assertEqual(config["grade"], {key: grade[key] for key in ("contrast", "brightness", "saturation")})
        self.assertEqual(config["analysis"], grade)

    def test_adaptive_preview_requires_resolved_color_snapshot(self):
        with self.assertRaisesRegex(ValueError, "error.analysis_stale"):
            self.service.request_preview({"source": str(self.source), "color_mode": "adaptive"})
        self.assertIsNone(self.service.preview_task)

    def test_reorder_is_atomic_durable_and_rejects_stale_lists(self):
        first, second, third = (self.create(str(i)) for i in range(3))
        self.service.reorder([third, first, second])
        self.assertEqual([j["id"] for j in self.reload().snapshot()["jobs"]], [third, first, second])
        for invalid in ([first, first, third], [first, second], [first, second, "missing"]):
            with self.assertRaisesRegex(ValueError, "error.queue_changed"):
                self.service.reorder(invalid)
        before = copy.deepcopy(self.service.jobs)
        with patch.object(self.service.store, "save_many", side_effect=OSError("disk failure")), self.assertRaises(OSError):
            self.service.reorder([first, second, third])
        self.assertEqual(self.service.jobs, before)

    def test_keyboard_move_preserves_active_jobs_and_is_durable(self):
        first, second, third = [self.create(str(i)) for i in range(3)]
        self.service.active_id = first
        self.service.move(third, -1)
        self.assertEqual([j['id'] for j in self.reload().snapshot()['jobs']], [first, third, second])
        for identifier, direction in [(first, 1), (third, -1), (second, 1), (second, True), (second, 2)]:
            with self.subTest(identifier=identifier, direction=direction), self.assertRaises(ValueError):
                self.service.move(identifier, direction)
        before = copy.deepcopy(self.service.jobs)
        with patch.object(self.service.store, 'save_many', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.service.move(second, -1)
        self.assertEqual(self.service.jobs, before)

    def test_snapshot_actions_match_lifecycle_and_lock(self):
        identifier = self.create()
        cases = {
            'queued': {'details', 'edit', 'preview', 'remove', 'reorder'},
            'running': {'details', 'pause', 'cancel'},
            'pause_requested': {'details', 'cancel'},
            'cancel_requested': {'details'},
            'paused': {'details', 'cancel', 'resume', 'cleanup_resume'},
            'completed': {'details', 'remove', 'open_output'},
            'failed': {'details', 'remove', 'retry'},
            'cancelled': {'details', 'remove'},
            'editing': {'details'},
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.service.update(identifier, status=status)
                self.assertEqual(set(self.service.snapshot()['jobs'][0]['actions']), expected)
        self.service.update(identifier, status='queued')
        self.service.active_id = identifier
        self.assertEqual(self.service.snapshot()['jobs'][0]['actions'], ['details'])
        self.service.active_id = None
        self.service.update(identifier, status='paused', resume_deleted=True)
        self.assertEqual(set(self.service.snapshot()['jobs'][0]['actions']), {'details', 'cancel'})

    def preview_values(self, name='preview'):
        return {'source':str(self.source), 'output':str(self.root / (name + '.mkv')), 'color_mode':'custom',
                'gpu_id':0, 'custom':{'contrast':1.07, 'brightness':0.01, 'saturation':1.02},
                'upscale':{'scale':2}, 'denoise':{'enabled':False, 'strength':3}}

    def test_draft_check_does_not_enqueue_render_or_repeat_analysis(self):
        with patch.object(self.service, '_analyze_config', side_effect=AssertionError('Unexpected analysis')):
            checks = self.service.check_draft(self.preview_values())
        self.assertTrue(checks['volumes'])
        self.assertFalse(self.service.jobs)
        self.assertIsNone(self.service.preview_task)
        self.assertFalse(list(self.service.data.glob('preflight-*')))
        values = self.preview_values()
        values['color_mode'] = 'adaptive'
        with self.assertRaisesRegex(ValueError, 'error.analysis_stale'):
            self.service.check_draft(values)

    def test_mp4_job_requires_track_loss_confirmation_and_keeps_snapshot(self):
        self.media['streams'] = ['audio', 'subtitle', 'attachment']
        self.media['track_codecs'] = [{'type':'audio', 'codec':'aac'},
                                      {'type':'subtitle', 'codec':'ass'},
                                      {'type':'attachment', 'codec':'ttf'}]
        values = self.preview_values('mp4-output')
        values['output'] = str(self.root / 'mp4-output.mp4')
        with self.assertRaisesRegex(ValueError, 'error.mp4_confirm_required'):
            self.service.check_draft(values)
        values['drop_tracks_confirmed'] = True
        self.assertTrue(self.service.check_draft(values)['volumes'])
        token = self.prepare_preview(values)
        identifier = self.service.commit_preview(token)
        job = self.reload().jobs[identifier]
        self.assertEqual(Path(job['output']).suffix, '.mp4')
        self.assertTrue(job['configuration']['drop_tracks_confirmed'])
        self.assertEqual(self.service._partial_path(job).suffix, '.mp4')

    def test_draft_check_output_conflict_and_saved_edit_guard(self):
        identifier = self.create()
        values = self.preview_values()
        values['output'] = self.service.jobs[identifier]['output']
        with self.assertRaisesRegex(ValueError, 'error.output_reserved'):
            self.service.check_draft(values)
        lease = self.service.begin_edit(identifier)
        editing = {'id':identifier, 'token':lease['edit_token']}
        self.assertTrue(self.service.check_draft({}, editing, saved=True)['volumes'])
        self.service.end_edit(identifier, editing['token'])
        with self.assertRaisesRegex(ValueError, 'error.job_locked'):
            self.service.check_draft({}, editing, saved=True)

    def prepare_preview(self, values=None, editing=None):
        token = self.service.request_preview(values or self.preview_values(), 0, 3, editing)
        with patch('borasuki.service.render_preview', return_value={'files':{}, 'fps':24, 'total_frames':12, 'width':480, 'height':270}):
            self.service._run_preview(self.service.preview_task)
        self.assertEqual(self.service.preview_task['status'], 'ready')
        return token

    def test_preview_commit_uses_exact_snapshot_and_is_single_use(self):
        token = self.prepare_preview()
        before = copy.deepcopy(self.service.preview_task['job']['configuration'])
        self.service.save_settings({'default_gpu':1})
        identifier = self.service.commit_preview(token)
        self.assertEqual(self.reload().jobs[identifier]['configuration'], before)
        with self.assertRaisesRegex(ValueError, 'error.preview_stale'):
            self.service.commit_preview(token)
        self.assertFalse(Path(before['output']).exists())

    def test_preview_commit_rechecks_output_and_source(self):
        token = self.prepare_preview()
        output = Path(self.service.preview_task['job']['output'])
        output.write_bytes(b'owned by another process')
        with self.assertRaisesRegex(ValueError, 'error.output_exists'):
            self.service.commit_preview(token)
        self.assertEqual(output.read_bytes(), b'owned by another process')
        output.unlink()
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'error.source_changed'):
            self.service.commit_preview(token)
        self.assertEqual(self.service.jobs, {})

    def test_preview_edit_requires_current_reservation(self):
        identifier = self.create()
        lease = self.service.begin_edit(identifier)
        values = self.preview_values()
        values['output'] = lease['output']
        token = self.prepare_preview(values, {'id':identifier, 'token':lease['edit_token']})
        config = copy.deepcopy(self.service.preview_task['job']['configuration'])
        self.assertEqual(self.service.commit_preview(token), identifier)
        self.assertEqual(self.service.jobs[identifier]['configuration'], config)
        self.assertEqual(self.service.jobs[identifier]['status'], 'queued')
        self.assertEqual(self.service.jobs[identifier]['revision'], lease['revision'] + 1)
        self.assertEqual(self.service._owned_work(self.service.jobs[identifier]), Path(self.service.jobs[identifier]['work']))
        lease = self.service.begin_edit(identifier)
        token = self.prepare_preview(values, {'id':identifier, 'token':lease['edit_token']})
        self.service.end_edit(identifier, lease['edit_token'])
        with self.assertRaisesRegex(ValueError, 'error.job_locked'):
            self.service.commit_preview(token)

    def test_preview_cancel_does_not_pause_queued_jobs(self):
        identifier = self.create()
        token = self.service.request_preview(self.preview_values())
        with self.assertRaisesRegex(ValueError, 'error.preview_busy'):
            self.service.request_preview(self.preview_values())
        self.service.cancel_preview(token)
        self.assertEqual(self.service.preview_task['status'], 'cancelled')
        self.assertEqual(self.service.jobs[identifier]['status'], 'queued')
        with self.assertRaisesRegex(ValueError, 'error.preview_stale'):
            self.service.commit_preview(token)

    def test_preview_waits_for_gpu_and_resume_cannot_overlap_it(self):
        identifier = self.create()
        rendering, previewing, release = threading.Event(), threading.Event(), threading.Event()
        seen = []
        def execute(job, stop, update):
            seen.append('render')
            rendering.set()
            if not stop.wait(3):
                raise RuntimeError('Render was not paused')
            seen.append('render-stopped')
            raise Interrupted()
        def preview(job, start, duration, stop, update):
            seen.append('preview')
            previewing.set()
            release.wait(3)
            return {'files':{}, 'fps':24, 'total_frames':12, 'width':480, 'height':270}
        with patch('borasuki.service.execute', side_effect=execute), patch('borasuki.service.render_preview', side_effect=preview):
            self.service.worker.start()
            self.assertTrue(rendering.wait(3))
            self.service.request_preview(self.preview_values())
            self.assertEqual(self.service.preview_task['status'], 'waiting')
            self.assertFalse(self.service.stop.is_set())
            self.service.save_settings({'queue_enabled':False})
            self.service.action(identifier, 'pause')
            self.assertTrue(previewing.wait(3))
            self.assertEqual(seen, ['render', 'render-stopped', 'preview'])
            self.service.action(identifier, 'resume')
            self.assertEqual(self.service.jobs[identifier]['status'], 'queued')
            self.assertEqual(len(seen), 3)
            release.set()
            self.service.shutdown()

    def test_preview_range_validation(self):
        for start, duration in [(-1,3), (0,0), (0,6), (float('nan'),3), (0,float('inf')), (True,3)]:
            with self.subTest(start=start, duration=duration), self.assertRaisesRegex(ValueError, 'error.preview_range'):
                self.service.request_preview(self.preview_values(), start, duration)

    def test_saved_job_preview_keeps_exact_configuration(self):
        identifier = self.create(mode='custom')
        original = copy.deepcopy(self.service.jobs[identifier]['configuration'])
        lease = self.service.begin_edit(identifier)
        token = self.service.request_preview({}, editing={'id':identifier, 'token':lease['edit_token']}, saved=True)
        with patch('borasuki.service.render_preview', return_value={'files':{}, 'fps':24, 'total_frames':12, 'width':480, 'height':270}):
            self.service._run_preview(self.service.preview_task)
        self.assertEqual(self.service.preview_task['status'], 'ready')
        self.assertEqual(self.service.preview_task['job']['configuration'], original)
        self.assertEqual(self.service.commit_preview(token), identifier)
        self.assertEqual(self.service.jobs[identifier]['configuration'], original)

    def test_edit_reservation_requires_token_and_invalidates_checkpoints(self):
        identifier = self.create()
        before = copy.deepcopy(self.service.jobs[identifier])
        lease = self.service.begin_edit(identifier)
        with self.assertRaisesRegex(ValueError, "error.job_locked"):
            self.service.end_edit(identifier, "wrong")
        with self.assertRaises(ValueError):
            self.service.action(identifier, "resume")
        self.service.end_edit(identifier, lease["edit_token"], {"source": str(self.source), "output": str(self.root / "edited.mkv"),
                              "color_mode": "original", "gpu_id": 1, "custom": {},
                              "upscale": {"scale": 2}, "denoise": {"enabled": False, "strength": 3}})
        after = self.service.jobs[identifier]
        self.assertEqual(after["status"], "queued")
        self.assertEqual(after["configuration"]["gpu_id"], 1)
        self.assertEqual(after["frames"], 0)
        self.assertNotEqual(after["work"], before["work"])
        self.assertEqual(after["order"], before["order"])
        self.assertEqual(after["configuration"]["denoise"], {"enabled": False, "strength": 3})
        self.assertEqual(after["model"]["noise"], -1)

    def test_failed_edit_preserves_reservation_and_old_configuration(self):
        identifier = self.create()
        lease = self.service.begin_edit(identifier)
        with self.assertRaises(ValueError):
            self.service.end_edit(identifier, lease["edit_token"], {"source": str(self.source), "output": "bad", "color_mode": "custom", "gpu_id": 0, "custom": {},
                                  "upscale": {"scale": 2}, "denoise": {"enabled": True, "strength": 1}})
        self.assertEqual(self.service.jobs[identifier]["configuration"], lease["configuration"])
        self.assertEqual(self.service.jobs[identifier]["status"], "editing")
        self.service.end_edit(identifier, lease["edit_token"])
        self.assertEqual(self.service.jobs[identifier]["status"], "queued")

    def test_clear_queue_only_removes_waiting_records(self):
        ids = [self.create(str(i)) for i in range(4)]
        self.pause_running(ids[1])
        self.service.active_id = ids[0]
        self.service.begin_edit(ids[2])
        with self.assertRaises(ValueError):
            self.service.remove("queue")
        self.assertEqual(self.service.remove("queue", confirmed=True), 1)
        self.assertEqual(set(self.service.jobs), set(ids[:3]))

    def test_history_clear_keeps_source_output_and_temporary_files(self):
        identifier = self.create()
        job = self.service.jobs[identifier]
        output, temporary = Path(job["output"]), Path(job["work"]) / "checkpoint.mkv"
        output.write_bytes(b"output")
        temporary.write_bytes(b"checkpoint")
        self.service.update(identifier, status="completed")
        self.service.remove("history", identifier, True)
        for path in (self.source, output, temporary):
            self.assertTrue(path.exists())
        self.assertEqual(self.reload().jobs, {})

    def test_retention_requires_consent_and_never_delete_is_respected(self):
        identifier = self.create()
        self.pause_running(identifier)
        expires = self.service.jobs[identifier]["paused_at"] + 30 * 86400
        with self.assertRaisesRegex(ValueError, "error.confirmation"):
            self.service.save_settings({"resume_retention": 30})
        self.service.expire_resume(expires + 1)
        self.assertFalse(self.service.jobs[identifier]["resume_deleted"])
        self.service.save_settings({"resume_retention": 30}, confirmed=True)
        self.service.save_settings({"resume_retention": "never"})
        self.service.expire_resume(expires + 1)
        self.assertFalse(self.service.jobs[identifier]["resume_deleted"])

    def test_retention_boundary_invalidates_resume_without_deleting_record(self):
        identifier = self.create()
        self.pause_running(identifier)
        work = Path(self.service.jobs[identifier]["work"])
        (work / "segment.mkv").write_bytes(b"checkpoint")
        self.service.save_settings({"resume_retention": 30}, confirmed=True)
        expires = self.service.jobs[identifier]["paused_at"] + 30 * 86400
        self.service.expire_resume(expires - 1)
        self.assertTrue(work.exists())
        self.service.expire_resume(expires)
        self.assertFalse(work.exists())
        self.assertTrue(self.source.exists())
        self.assertEqual(self.service.jobs[identifier]["status"], "paused")
        with self.assertRaisesRegex(ValueError, "error.resume_deleted"):
            self.service.action(identifier, "resume")

    def test_cleanup_never_touches_active_or_foreign_directories(self):
        identifier = self.create()
        self.pause_running(identifier)
        self.service.active_id = identifier
        with self.assertRaisesRegex(ValueError, "error.job_locked"):
            self.service.cleanup_resume(identifier, True)
        self.service.active_id = None
        self.service.update(identifier, work=str(self.root))
        with self.assertRaisesRegex(ValueError, "error.unsafe_cleanup"):
            self.service.cleanup_resume(identifier, True)
        self.assertTrue(self.source.exists())

    def test_failed_cleanup_is_not_misrepresented_as_resumable(self):
        identifier = self.create()
        self.pause_running(identifier)
        with patch("borasuki.service.shutil.rmtree", side_effect=OSError("file in use")), self.assertRaises(OSError):
            self.service.cleanup_resume(identifier, True)
        self.assertTrue(self.reload().jobs[identifier]["resume_deleted"])

    def test_pause_again_resets_retention_but_reading_details_does_not(self):
        identifier = self.create()
        with patch("borasuki.service.time.time", return_value=100):
            self.pause_running(identifier)
        self.service.snapshot()
        self.assertEqual(self.service.jobs[identifier]["paused_at"], 100)
        self.service.action(identifier, "resume")
        with patch("borasuki.service.time.time", return_value=200):
            self.pause_running(identifier)
        self.assertEqual(self.service.jobs[identifier]["paused_at"], 200)

    def test_failed_write_does_not_change_memory_or_signal_cancel(self):
        identifier = self.create()
        self.service.active_id = identifier
        self.service.update(identifier, status="running")
        with patch.object(self.service.store, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.service.action(identifier, "cancel", True)
        self.assertFalse(self.service.stop.is_set())
        self.assertEqual(self.service.jobs[identifier]["status"], "running")

    def test_fresh_preflight_checks_source_output_gpu_and_both_disk_locations(self):
        identifier = self.create()
        job = self.service.jobs[identifier]
        usage = type("Usage", (), {"free": RESERVE * 10})()
        with patch("borasuki.preflight.shutil.disk_usage", return_value=usage) as disk:
            result = check(job, self.runtime)
        self.assertEqual(disk.call_count, 2)
        self.assertTrue(result["approximate"])
        self.assertEqual(sum(v["estimated"] for v in result["volumes"]), RESERVE * 2)
        with patch("borasuki.preflight.shutil.disk_usage", return_value=type("Usage", (), {"free": 1})()), self.assertRaisesRegex(ValueError, "error.disk_space"):
            check(job, self.runtime)
        Path(job["output"]).write_bytes(b"another output")
        with self.assertRaisesRegex(ValueError, "error.output_exists"):
            check(job, self.runtime)
        Path(job["output"]).unlink()
        changed = copy.deepcopy(self.runtime)
        changed["gpus"][0]["driver"] = "new"
        with self.assertRaisesRegex(ValueError, "error.runtime_changed"):
            check(job, changed)
        self.source.write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "error.source_changed"):
            check(job, self.runtime)

    def test_worker_obeys_durable_order_and_skips_edit_reservation(self):
        first, second, third = [self.create(str(i)) for i in range(3)]
        self.service.reorder([third, second, first])
        self.service.begin_edit(second)
        seen, done = [], threading.Event()
        def runner(job, stop, update):
            seen.append(job["id"])
            update(status="completed", stage="completed")
            if len(seen) == 2:
                self.service.closing = True
                done.set()
        with patch("borasuki.service.execute", side_effect=runner):
            self.service.worker.start()
            self.assertTrue(done.wait(3))
            self.service.worker.join(3)
        self.assertEqual(seen, [third, first])
        self.assertEqual(self.service.jobs[second]["status"], "editing")

    def test_start_checks_again_after_enqueue(self):
        identifier = self.create()
        self.source.write_bytes(b"changed during queue wait")
        with patch("borasuki.service.execute") as execute:
            self.service.worker.start()
            deadline = time.monotonic() + 3
            while self.service.jobs[identifier]["status"] != "failed" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.service.shutdown()
        execute.assert_not_called()
        self.assertEqual(self.service.jobs[identifier]["error"], "error.source_changed")

    def test_color_modes_do_not_select_scale_or_denoise(self):
        automatic = {"contrast": 1.025, "brightness": 0.0, "saturation": 1.01}
        def analyze(args, **kwargs):
            path = next(arg[4:] for arg in args if arg.startswith("job="))
            config = json.loads(Path(path).read_text())
            atomic_json(Path(config["work"]) / "analysis.json", automatic)
            return b""
        for mode in ("reference", "original", "custom", "adaptive"):
            adaptive = None
            if mode == "adaptive":
                with patch.object(self.service, "_analyze_config", return_value=automatic):
                    token = self.service.request_analysis(str(self.source))
                    self.service.analysis_task["future"].result(timeout=3)
                adaptive = {"token": token}
            for noise in (-1, 1, 2, 3):
                with self.subTest(color=mode, noise=noise), patch("borasuki.service.ProcessGroup.capture", side_effect=analyze):
                    denoise = {"enabled": noise != -1, "strength": max(1, noise)}
                    identifier = self.service.create(str(self.source), str(self.root / f"{mode}-{noise}.mkv"), mode, 0,
                                                     {"contrast": 1.02, "brightness": 0.01, "saturation": 1.01}, {"scale": 2}, denoise, adaptive)
                    job = self.service.jobs[identifier]
                    config = job["configuration"]
                    self.assertNotIn("mode", config)
                    self.assertEqual(config["color_mode"], mode)
                    self.assertEqual(config["upscale"], {"scale": 2})
                    self.assertEqual(config["denoise"], denoise)
                    self.assertEqual(cugan_options(config)["noise"], noise)
                    self.assertEqual(config["model"]["file"], cugan_model_name(noise))
                    if mode == "adaptive":
                        self.assertEqual(config["grade"], automatic)
                    check(job, self.runtime)

    def test_new_1080p_tile_is_snapshotted_without_changing_processing_choices(self):
        self.media.update(width=1920, height=1080)
        identifier = self.create()
        config = copy.deepcopy(self.service.jobs[identifier]['configuration'])
        self.assertEqual(config['tile'], [486, 276])
        self.assertEqual(config['denoise'], {'enabled': True, 'strength': 1})
        self.assertEqual(config['encoding'], {'codec': 'hevc_nvenc', 'preset': 'p7', 'cq': 15})
        self.assertTrue(config['require_engine_prepared'])
        self.assertEqual(config['encoding']['cq'], 15)
        self.runtime['gpus'][0]['free'] = 1000
        self.assertEqual(self.reload().jobs[identifier]['configuration'], config)
        low = self.create('low-memory')
        self.assertEqual(self.service.jobs[low]['tile'], [240, 136])

    def test_existing_1080p_tile_is_not_migrated(self):
        identifier = self.create()
        job = self.service.jobs[identifier]
        for config in (job, job['configuration']):
            config['media'].update(width=1920, height=1080)
            config['tile'] = [480, 270]
        self.service.store.save(job)
        restored = self.reload().jobs[identifier]
        self.assertEqual(restored['tile'], [480, 270])
        self.assertEqual(restored['configuration']['tile'], [480, 270])

    def test_portrait_tile_is_only_applied_to_new_configurations(self):
        self.media.update(width=1080, height=1920)
        identifier = self.create('portrait')
        job = self.service.jobs[identifier]
        self.assertEqual(job['configuration']['tile'], [276, 486])
        for config in (job, job['configuration']):
            config['tile'] = [480, 270]
        self.service.store.save(job)
        restored = self.reload().jobs[identifier]
        self.assertEqual(restored['tile'], [480, 270])
        self.assertEqual(restored['configuration']['tile'], [480, 270])

    def test_selected_model_is_validated_instead_of_fixed_noise_one(self):
        model = Path(self.runtime["plugins"]) / "models/cugan" / cugan_model_name(3)
        identifier = self.service.create(str(self.source), str(self.root / "strong.mkv"), "original", 0, {},
                                         {"scale": 2}, {"enabled": True, "strength": 3})
        model.unlink()
        with self.assertRaisesRegex(ValueError, "error.denoise_model_missing"):
            check(self.service.jobs[identifier], self.runtime)
        with self.assertRaisesRegex(ValueError, "error.denoise_model_missing"):
            self.service.create(str(self.source), str(self.root / "new.mkv"), "custom", 0,
                                {"contrast": 1, "brightness": 0, "saturation": 1},
                                {"scale": 2}, {"enabled": True, "strength": 3})
        self.assertEqual(len(self.service.jobs), 1)

    def test_invalid_denoise_or_scale_cannot_be_queued(self):
        for upscale, denoise in [({"scale": 4}, {"enabled": True, "strength": 1}),
                                 ({"scale": True}, {"enabled": True, "strength": 1}),
                                 ({"scale": 2}, {"enabled": 1, "strength": 1}),
                                 ({"scale": 2}, {"enabled": False, "strength": 9}),
                                 ({"scale": 2}, {"enabled": True, "strength": 1.5})]:
            with self.subTest(upscale=upscale, denoise=denoise), self.assertRaises(ValueError):
                processing_settings(upscale, denoise)

    def test_legacy_job_migration_preserves_actual_processing_and_work(self):
        identifier = self.create()
        legacy = copy.deepcopy(self.service.jobs[identifier])
        for config in (legacy, legacy["configuration"]):
            config["mode"] = config.pop("color_mode")
            config.pop("upscale")
            config.pop("denoise")
        self.service.store.save(legacy)
        migrated = self.reload().jobs[identifier]
        self.assertEqual(migrated["work"], legacy["work"])
        self.assertEqual(migrated["grade"], legacy["grade"])
        self.assertEqual(migrated["fingerprint"], legacy["fingerprint"])
        self.assertEqual(migrated["configuration"]["upscale"], {"scale": 2})
        self.assertEqual(migrated["configuration"]["denoise"], {"enabled": True, "strength": 1})
        self.assertEqual(migrated["configuration"]["color_mode"], "reference")
        self.assertNotIn("mode", migrated["configuration"])


if __name__ == "__main__":
    unittest.main()
