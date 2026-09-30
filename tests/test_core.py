import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from fractions import Fraction
from unittest.mock import patch

import numpy as np

from borasuki.enhance import analyze, transform
from borasuki.process import Interrupted, ProcessError, ProcessGroup
from borasuki.storage import JobStore, atomic_json
from borasuki.pipeline import inspect_source, validate_timecodes


class StorageTests(unittest.TestCase):
    def test_undefined_average_fps_has_user_facing_error(self):
        info = {"streams": [{"codec_type": "video", "avg_frame_rate": "0/0",
                             "r_frame_rate": "24000/1001"}]}
        with patch("borasuki.pipeline.probe", return_value=info):
            with self.assertRaisesRegex(ValueError, "error.vfr"):
                inspect_source(Path("sample.mkv"), {}, threading.Event())

    def test_cut_tail_timing_is_normalized_only_with_matching_duration(self):
        fps = Fraction(24000, 1001)
        interval = 1000 / float(fps)
        frames = 403
        original = [100 + index * interval for index in range(frames)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'timecodes.txt'
            for tail in (1, 2):
                values = original.copy()
                for index in range(frames - tail, frames):
                    values[index] += (index - (frames - tail) + 1) * interval
                path.write_text('\n'.join(str(value) for value in values))
                self.assertEqual(validate_timecodes(path, frames, fps, frames / float(fps)), tail)
                for duration in (None, frames / float(fps) + 0.1):
                    with self.assertRaisesRegex(ValueError, 'error.vfr'):
                        validate_timecodes(path, frames, fps, duration)
            path.write_text('\n'.join(str(value) for value in original))
            self.assertEqual(validate_timecodes(path, frames, fps), 0)

    def test_cut_tail_policy_rejects_real_vfr_and_invalid_timestamps(self):
        frames, fps = 60, Fraction(25)
        original = [index * 40 for index in range(frames)]
        variants = []
        for start, offset in ((30, 40), (57, 40), (59, 120), (59, 20), (59, -20)):
            variants.append([value + (offset if index >= start else 0) for index, value in enumerate(original)])
        for bad in (float('nan'), float('inf'), original[-2], -1):
            variants.append([*original[:-1], bad])
        variants.append(original[:-1])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'timecodes.txt'
            for values in variants:
                with self.subTest(values=values[-4:]):
                    path.write_text('\n'.join(str(value) for value in values))
                    with self.assertRaisesRegex(ValueError, 'error.vfr'):
                        validate_timecodes(path, frames, fps, frames / float(fps))
            path.write_text('0\n40\n120')
            with self.assertRaisesRegex(ValueError, 'error.vfr'):
                validate_timecodes(path, 3, fps, 3 / float(fps))

    def test_vfr_is_detected_even_when_average_fps_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "timecodes.txt"
            path.write_text("# timecode format v2\n0\n40\n100\n120\n")
            with self.assertRaisesRegex(ValueError, "error.vfr"):
                validate_timecodes(path, 4, Fraction(25))
            path.write_text("# timecode format v2\n0\n42\n83\n125\n")
            validate_timecodes(path, 4, Fraction(24))

    def test_interrupted_settings_write_keeps_previous_value(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            atomic_json(path, {"language": "tr"})
            with patch("borasuki.storage.os.replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
                atomic_json(path, {"language": "en"})
            self.assertEqual(json.loads(path.read_text()), {"language": "tr"})
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_nonserializable_state_does_not_replace_committed_job(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.db")
            job = {"id": "a", "status": "paused", "frames": 240}
            store.save(job)
            with self.assertRaises(ValueError):
                store.save({**job, "frames": float("nan")})
            self.assertEqual(JobStore(store.path).load(), [job])

    def test_sql_special_characters_remain_data(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.db")
            job = {"id": "'; DROP TABLE jobs; --", "name": "Türkçe"}
            store.save(job)
            self.assertEqual(store.load(), [job])


class EnhanceTests(unittest.TestCase):
    def test_original_transform_is_identity(self):
        pixels = np.random.default_rng(1).random((20, 20, 3)).astype(np.float32)
        np.testing.assert_allclose(transform(pixels, 1, 0, 1), pixels, atol=1e-7)

    def test_dark_and_bright_styles_are_preserved(self):
        for level in (0.03, 0.9):
            result = analyze([np.full((20, 20, 3), level)] * 3)
            self.assertEqual(result["reason"], "preserved")

    def test_bounded_deterministic_video_level_adjustment(self):
        sample = np.random.default_rng(42).uniform(0.15, 0.85, (60, 60, 3))
        result = analyze([sample] * 9)
        self.assertEqual(result, analyze([sample] * 9))
        self.assertLessEqual(result["contrast"], 1.03)
        self.assertLessEqual(result["saturation"], 1.02)
        self.assertEqual(result["brightness"], 0)

    def test_clipped_source_does_not_get_more_clipping(self):
        sample = np.random.default_rng(42).uniform(0.1, 0.9, (100, 100, 3))
        sample[::2, :, 0] = 1
        grade = analyze([sample] * 3)
        result = transform(sample, grade["contrast"], grade["brightness"], grade["saturation"])
        self.assertLessEqual(np.mean((result <= 0) | (result >= 1)), np.mean((sample <= 0.001) | (sample >= 0.999)) + 0.001)

    def test_nan_samples_are_rejected(self):
        with self.assertRaises(ValueError):
            analyze([np.full((10, 10, 3), np.nan)] * 3)


class ProcessTests(unittest.TestCase):
    def test_failure_propagates_stderr(self):
        with ProcessGroup(threading.Event()) as group, self.assertRaisesRegex(ProcessError, "diagnostic"):
            group.capture([sys.executable, "-c", "import sys; sys.stderr.write('diagnostic'); sys.exit(7)"])

    def test_already_cancelled_does_not_spawn(self):
        stop = threading.Event()
        stop.set()
        with ProcessGroup(stop) as group, self.assertRaises(Interrupted):
            group.capture([sys.executable, "-c", "pass"])
        self.assertEqual(group.children, [])

    def test_timeout_kills_process(self):
        with ProcessGroup(threading.Event()) as group, self.assertRaises(ProcessError):
            group.capture([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1)
        self.assertIsNotNone(group.children[0].poll())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object")
    def test_closing_group_kills_grandchild(self):
        with ProcessGroup(threading.Event()) as group:
            script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
            child = group.spawn([sys.executable, "-c", script], stdout=subprocess.PIPE)
            grandchild = int(child.stdout.readline())
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x100000, False, grandchild)
            self.assertTrue(handle)
        try:
            self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
        finally:
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
