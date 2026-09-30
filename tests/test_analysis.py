"""Selection-time color analysis, cancellation and snapshot acceptance."""

import copy
import threading
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace
import tempfile
from pathlib import Path
from fractions import Fraction

import test_queue
from borasuki.enhance import validate_grade
from borasuki.process import Interrupted
from borasuki.process import ProcessError
from borasuki.source import open_source
from borasuki.errors import failure_info, RenderDiagnostics
from borasuki.pipeline import progress_frame


GRADE = {"contrast": 1.012345, "brightness": 0, "saturation": 1.006789, "reason": "adjusted", "version": 1}


class AnalysisTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp

    def analyze(self):
        with patch.object(self.service, "_analyze_config", return_value=copy.deepcopy(GRADE)):
            token = self.service.request_analysis(str(self.source))
            self.service.analysis_task["future"].result(timeout=3)
        self.assertEqual(self.service.analysis_task["status"], "ready")
        return token

    def values(self, token, overrides=None):
        return {"source": str(self.source), "output": str(self.root / "result.mkv"), "color_mode": "adaptive",
                "gpu_id": 0, "custom": {}, "upscale": {"scale": 2}, "denoise": {"enabled": False, "strength": 3},
                "adaptive": {"token": token, "overrides": overrides or {}}}

    def test_result_reused_exactly_and_only_explicit_color_override_applied(self):
        token = self.analyze()
        with patch.object(self.service, "_analyze_config", side_effect=AssertionError("Repeated analysis")):
            identifier = self.service.create(**self.values(token, {"brightness": 0.01}))
        job = self.service.jobs[identifier]
        self.assertEqual(job["grade"], {"contrast": GRADE["contrast"], "brightness": 0.01, "saturation": GRADE["saturation"]})
        self.assertEqual(job["analysis"], GRADE)
        self.assertEqual(job["color_overrides"], {"brightness": 0.01})
        self.assertEqual(job["configuration"]["grade"], job["grade"])
        self.assertEqual(job["model"]["noise"], -1)
        self.assertEqual(job["upscale"]["scale"], 2)
        self.service.cancel_analysis(token)
        self.assertEqual(job["configuration"]["analysis"], GRADE)
        edited = self.service.begin_edit(identifier)
        with patch.object(self.service, "_analyze_config", side_effect=AssertionError("Reanalyzed saved job")):
            new = self.service.request_analysis(str(self.source), {"id": identifier, "token": edited["edit_token"]})
        self.assertNotEqual(new, token)
        self.assertEqual(self.service.snapshot()["analysis"]["overrides"], {"brightness": 0.01})

    def test_invalid_stale_and_changed_source_rejected(self):
        token = self.analyze()
        for overrides in ({"contrast": float("nan")}, {"brightness": True}, {"saturation": 9}, {"scale": 4}):
            with self.assertRaisesRegex(ValueError, "error.invalid_settings"):
                self.service.create(**self.values(token, overrides))
        self.source.write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "error.source_changed"):
            self.service.create(**self.values(token))
        self.service.cancel_analysis(token)
        with self.assertRaisesRegex(ValueError, "error.analysis_stale"):
            self.service.create(**self.values(token))
        self.assertFalse(self.service.jobs)

    def test_sampling_cancelled_without_changing_running_job(self):
        job = test_queue.QueueTests.create(self)
        self.service.active_id = job
        self.service.update(job, status="running")
        started = threading.Event()

        def sampling(config, stop, update=None):
            started.set()
            if not stop.wait(3):
                raise AssertionError("Analysis not cancelled")
            raise Interrupted()

        with patch.object(self.service, "_analyze_config", side_effect=sampling):
            token = self.service.request_analysis(str(self.source))
            self.assertTrue(started.wait(3))
            task = self.service.analysis_task
            self.assertEqual(self.service.snapshot()["analysis"]["status"], "analyzing")
            self.service.cancel_analysis(token)
            task["future"].result(timeout=3)
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(self.service.jobs[job]["status"], "running")
        self.assertFalse(self.service.stop.is_set())
        self.assertFalse(list(self.service.data.glob("color-*")))

    def test_waiting_preview_pins_result_even_if_next_analysis_replaces_it(self):
        token = self.analyze()
        preview = self.service.request_preview(self.values(token, {"contrast": 1.1}))
        self.service.cancel_analysis(token)
        task = self.service.preview_task
        with patch("borasuki.service.render_preview", return_value={"files": {}}), \
             patch.object(self.service, "_analyze_config", side_effect=AssertionError("Repeated analysis")):
            self.service._run_preview(task)
        self.assertEqual(task["status"], "ready")
        identifier = self.service.commit_preview(preview)
        self.assertEqual(self.service.jobs[identifier]["grade"]["contrast"], 1.1)
        self.assertEqual(self.service.jobs[identifier]["grade"]["saturation"], GRADE["saturation"])

    def test_failure_visible_and_late_cancel_does_not_cancel_new_result(self):
        with patch.object(self.service, "_analyze_config", side_effect=ValueError("error.source_access")):
            old = self.service.request_analysis(str(self.source))
            self.service.analysis_task["future"].result(timeout=3)
        self.assertEqual(self.service.snapshot()["analysis"]["error"], "error.source_access")
        token = self.analyze()
        self.service.cancel_analysis(old)
        self.assertEqual(self.service.analysis_task["id"], token)
        self.assertEqual(self.service.analysis_task["status"], "ready")

    def test_grade_validation(self):
        for invalid in (None, [], {}, {"contrast": 1, "brightness": 0}):
            with self.assertRaises(ValueError):
                validate_grade(invalid)
        self.assertEqual(validate_grade({}, partial=True), {})

    def test_decoder_failure_preserves_cause_and_appropriate_recovery(self):
        cause = "Source: Frame accurate seeking is not possible in this file"
        with patch.object(self.service, "_analyze_config", side_effect=ProcessError(cause)):
            self.service.request_analysis(str(self.source))
            self.service.analysis_task["future"].result(timeout=3)
        task = self.service.snapshot()["analysis"]
        self.assertEqual(task["error"], "error.video_seek")
        self.assertEqual(task["failure"]["detail"], cause)
        self.assertEqual(task["failure"]["recovery"], "source")
        self.assertFalse(task["failure"]["retryable"])


class DecoderTests(unittest.TestCase):
    def test_embedded_timing_error_reports_the_cause_without_false_retry(self):
        for code in ('error.vfr', 'error.timestamp'):
            detail = f'Script evaluation failed:\nPython exception: {code}\n\nTraceback (most recent call last):\nValueError: {code}\n'
            failure = failure_info(RuntimeError(detail), 'error.analysis_failed')
            self.assertEqual(failure['code'], code)
            self.assertEqual(failure['recovery'], 'source')
            self.assertFalse(failure['retryable'])
            self.assertEqual(failure['detail'], detail)
        failure = failure_info(RuntimeError('Cannot open C:/error.vfr/video.mp4'), 'error.analysis_failed')
        self.assertEqual(failure['code'], 'error.analysis_failed')

    def test_cut_tail_normalizes_even_when_decoder_reports_nominal_fps(self):
        with tempfile.TemporaryDirectory() as folder:
            frames, fps = 60, Fraction(24000, 1001)
            interval = 1000 / float(fps)
            values = [index * interval for index in range(frames)]
            values[-2] += interval
            values[-1] += 2 * interval
            (Path(folder) / 'source.timecodes.txt').write_text('\n'.join(str(value) for value in values))
            clip = SimpleNamespace(num_frames=frames, fps_num=fps.numerator, fps_den=fps.denominator)
            core = SimpleNamespace(ffms2=SimpleNamespace(Source=Mock(return_value=clip)),
                                   std=SimpleNamespace(AssumeFPS=Mock(return_value='normalized')))
            config = {'source': 'video', 'work': folder, 'media': {'video_index': 0,
                      'fps_num': fps.numerator, 'fps_den': fps.denominator, 'duration': frames / float(fps)}}
            self.assertEqual(open_source(core, config), ('normalized', 1))
            core.std.AssumeFPS.assert_called_once_with(clip, fpsnum=fps.numerator, fpsden=fps.denominator)

    def test_render_cause_survives_progress_flood(self):
        diagnostic = RenderDiagnostics()
        cause = 'Error: fwrite() call failed when writing video plane 0, errno: 22, frame: 38'
        diagnostic.add(cause)
        for number in range(500):
            diagnostic.add(f'frame={number}')
        failure = failure_info(RuntimeError(diagnostic.detail()))
        self.assertEqual(failure['code'], 'error.frame_transfer')
        self.assertIn(cause, failure['detail'])
        for evidence, code in [('No space left on device', 'error.disk_space'),
                               ('CUDA out of memory', 'error.gpu_memory'), ('Permission denied', 'error.file_access')]:
            self.assertEqual(failure_info(RuntimeError(evidence + '\n' + cause))['code'], code)

    def test_nominal_fps_requires_validating_every_frame_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            times = Path(folder) / 'source.timecodes.txt'
            times.write_text('# timecode format v2\n98\n139.71\n181.42\n')
            clip = SimpleNamespace(num_frames=3, fps_num=1000000, fps_den=41767)
            core = SimpleNamespace(ffms2=SimpleNamespace(Source=Mock(return_value=clip)), std=SimpleNamespace(AssumeFPS=Mock(return_value='normalized')))
            config = {'source':'video', 'media':{'video_index':0,'fps_num':24000,'fps_den':1001},'work':folder}
            self.assertEqual(open_source(core, config), ('normalized', 1))
            core.std.AssumeFPS.assert_called_once_with(clip, fpsnum=24000, fpsden=1001)
            core.std.AssumeFPS.reset_mock()
            times.write_text('# timecode format v2\n98\n139.71\n201.42\n')
            with self.assertRaisesRegex(ValueError, 'error.vfr'):
                open_source(core, config)
            core.std.AssumeFPS.assert_not_called()

    def test_progress_accepts_ffmpeg_summary_without_stopping_log_reader(self):
        self.assertEqual(progress_frame('frame=3'), 3)
        self.assertEqual(progress_frame('frame=    3 fps=0.0 q=16.9 Lsize=201KiB'), 3)
        for line in ('frame=N/A', 'frame=', 'fps=3'):
            self.assertIsNone(progress_frame(line))

    def test_only_known_seek_error_uses_safe_linear_fallback(self):
        source = Mock(side_effect=[RuntimeError("Source: Frame accurate seeking is not possible in this file"), "clip"])
        config = {"source": "video.mp4", "media": {"video_index": 2}, "work": "work"}
        clip, mode = open_source(SimpleNamespace(ffms2=SimpleNamespace(Source=source)), config)
        self.assertEqual((clip, mode), ("clip", 0))
        self.assertEqual([call.kwargs["seekmode"] for call in source.call_args_list], [1, 0])
        for call in source.call_args_list:
            self.assertEqual(call.kwargs["source"], config["source"])
            self.assertEqual(call.kwargs["track"], 2)

    def test_no_fallback_on_other_errors_and_no_infinite_retry(self):
        for cause, attempts in [("Decoder corrupt", 1), ("Frame accurate seeking is not possible in this file", 2)]:
            source = Mock(side_effect=RuntimeError(cause))
            with self.assertRaisesRegex(RuntimeError, cause):
                open_source(SimpleNamespace(ffms2=SimpleNamespace(Source=source)), {"source":"video", "media":{"video_index":0}, "work":"work"})
            self.assertEqual(source.call_count, attempts)

    def test_unknown_error_retains_details_and_timestamp_notes(self):
        failure = failure_info(RuntimeError("Unexpected decoder failure"), "error.analysis_failed")
        self.assertTrue(failure["retryable"])
        self.assertEqual(failure["detail"], "Unexpected decoder failure")
        error = ValueError("error.timestamp")
        error.add_note("Video start: 0.125 s")
        failure = failure_info(error)
        self.assertIn("0.125", failure["detail"])
        self.assertEqual(failure["recovery"], "source")


if __name__ == "__main__":
    unittest.main()
