"""Runtime prerequisite and scheduler guards without model compilation."""

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_queue
import borasuki.runtime as runtime_module
from borasuki.runtime import required_files, validation_signature
from borasuki.service import Service

LOAD_SETUP = Service._load_setup


class RuntimeDiscoveryTests(unittest.TestCase):
    def test_app_owned_runtime_takes_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / 'data'
            bundled = Path(root) / 'bundle'
            for base in (data, bundled):
                runtime = base / 'runtime'
                (runtime / 'plugins').mkdir(parents=True)
                (runtime / 'plugins' / 'vstrt.dll').write_bytes(b'plugin')
                (runtime / 'vspipe.exe').write_bytes(b'pipe')
                for name in ('ffmpeg', 'ffprobe'):
                    (runtime / f'{name}.exe').write_bytes(b'codec')
            owned_pipe = data / 'runtime' / 'python' / 'Lib' / 'site-packages' / 'vapoursynth' / 'vspipe.exe'
            owned_pipe.parent.mkdir(parents=True)
            owned_pipe.write_bytes(b'pipe')
            with patch.object(runtime_module, 'DATA', data), patch.object(runtime_module, 'ROOT', bundled), \
                    patch('borasuki.runtime.shutil.which', return_value=None):
                result = runtime_module.discover()
            self.assertEqual(result['plugins'], str(data / 'runtime' / 'plugins'))
            self.assertEqual(result['vspipe'], str(owned_pipe))
            self.assertEqual(result['ffmpeg'], str(data / 'runtime' / 'ffmpeg.exe'))


class SetupTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp

    def test_gate_blocks_creation_and_preview_before_validation(self):
        for status in ("waiting", "checking", "missing", "failed"):
            self.service.setup = {"status": status}
            with self.assertRaisesRegex(ValueError, "error.setup_required"):
                test_queue.QueueTests.create(self)
            with self.assertRaisesRegex(ValueError, "error.setup_required"):
                self.service.request_preview({})
        self.assertFalse(self.service.jobs)

    def test_success_persisted_failure_and_missing_never_mark_ready(self):
        result = {"signature": {"fixture": True}, "report": {"devices": [{"id": 0}]}}
        with patch("borasuki.service.validate_runtime", return_value=result):
            self.service._run_setup()
        self.assertEqual(self.service.setup["status"], "ready")
        self.assertEqual(json.loads((self.service.data / "runtime-validation.json").read_text()), result)
        self.assertEqual(LOAD_SETUP(self.service)["status"], "ready")
        self.service.request_setup()
        self.assertFalse((self.service.data / "runtime-validation.json").exists())
        self.assertEqual(LOAD_SETUP(self.service)["status"], "waiting")
        with patch("borasuki.service.validate_runtime", side_effect=RuntimeError("DLL missing")):
            self.service._run_setup()
        self.assertEqual(self.service.setup["status"], "failed")
        self.assertEqual(self.service.setup["error"], "error.setup_failed")
        self.assertIn('DLL missing', self.service.snapshot()['setup']['failure']['detail'])
        self.assertFalse((self.service.data / "runtime-validation.json").exists())
        with patch("borasuki.service.discover", return_value={**self.runtime, "ready": False, "missing": ["TensorRT"]}), \
                patch("borasuki.service.install_runtime", side_effect=ValueError('error.disk_space')):
            self.service._run_setup()
        self.assertEqual(self.service.setup["status"], "failed")
        self.assertEqual(self.service.setup['error'], 'error.disk_space')

    def test_worker_verifies_before_starting_queue(self):
        identifier = test_queue.QueueTests.create(self)
        checking, release, rendering = threading.Event(), threading.Event(), threading.Event()

        def verify(runtime, data, stop, update):
            checking.set()
            if not release.wait(3):
                raise AssertionError("Verification not released")
            return {"signature": {"fixture": True}}

        def execute(job, stop, update):
            rendering.set()
            update(status="completed")

        self.service.setup = {"status": "waiting"}
        with patch("borasuki.service.validate_runtime", side_effect=verify), patch("borasuki.service.execute", side_effect=execute):
            self.service.worker.start()
            self.service.wake.set()
            self.assertTrue(checking.wait(3))
            self.assertEqual(self.service.jobs[identifier]["status"], "queued")
            self.assertFalse(rendering.is_set())
            with self.assertRaisesRegex(ValueError, "error.setup_required"):
                self.service.request_preview({})
            release.set()
            self.assertTrue(rendering.wait(3))
            self.service.shutdown()

    def test_missing_runtime_installs_before_validation(self):
        missing = {**self.runtime, 'ready': False, 'missing': ['TensorRT']}
        result = {'signature': {'fixture': True}, 'report': {'devices': [{'id': 0}]}}
        with patch('borasuki.service.discover', return_value=missing), \
                patch('borasuki.service.install_runtime', return_value=self.runtime) as install, \
                patch('borasuki.service.validate_runtime', return_value=result) as validate:
            self.service._run_setup()
        install.assert_called_once()
        validate.assert_called_once()
        self.assertEqual(self.service.setup['status'], 'ready')

    def test_recheck_rejected_during_active_job_or_preview(self):
        identifier = test_queue.QueueTests.create(self)
        self.service.active_id = identifier
        with self.assertRaisesRegex(ValueError, "error.setup_busy"):
            self.service.request_setup()
        self.service.active_id = None
        for status in ("waiting", "preparing", "rendering"):
            self.service.preview_task = {"status": status, "stop": threading.Event()}
            with self.assertRaisesRegex(ValueError, "error.setup_busy"):
                self.service.request_setup()
        self.assertEqual(self.service.setup["status"], "ready")

    def test_signature_change_closes_gate_and_requests_revalidation(self):
        with patch("borasuki.service.validation_signature", return_value={"changed": True}):
            with self.assertRaisesRegex(ValueError, "error.setup_required"):
                self.service._require_setup(self.runtime)
        self.assertEqual(self.service.setup["status"], "waiting")
        self.assertTrue(self.service.wake.is_set())

    def test_all_denoise_models_in_signature_and_memory_ignored(self):
        files = required_files(self.runtime)
        models = [name for name in files if name.endswith(".onnx")]
        self.assertEqual(len(models), 4)
        for path in files.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(b"runtime fixture")
        before = validation_signature(self.runtime)
        changed = copy.deepcopy(self.runtime)
        changed["gpus"][0]["free"] = 10
        self.assertEqual(validation_signature(changed), before)
        files[models[-1]].write_bytes(b"different model")
        self.assertNotEqual(validation_signature(self.runtime), before)


if __name__ == "__main__":
    unittest.main()
