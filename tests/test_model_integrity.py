"""Required model files must be verified before loading renderer dependencies."""

import hashlib
import threading
import unittest
from unittest.mock import patch

import test_queue
from borasuki.profile import cugan_model_name
from borasuki.process import Interrupted
from borasuki.runtime import REQUIRED_NOISE, validate_models, validate_runtime


class ModelIntegrityTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp

    def hashes(self):
        return {noise: hashlib.sha256((self.root / 'plugins/models/cugan' / cugan_model_name(noise)).read_bytes()).hexdigest()
                for noise in REQUIRED_NOISE}

    def test_all_four_checked_and_corruption_identifies_exact_model(self):
        with patch('borasuki.runtime.CUGAN_SHA256', self.hashes()):
            report = validate_models(self.runtime, threading.Event())
            self.assertEqual(set(report), {cugan_model_name(noise) for noise in REQUIRED_NOISE})
            target = self.root / 'plugins/models/cugan' / cugan_model_name(3)
            target.write_bytes(b'corrupt model')
            with self.assertRaisesRegex(ValueError, 'error.model_integrity') as caught:
                validate_models(self.runtime, threading.Event())
            self.assertIn(target.name, caught.exception.__notes__[0])

    def test_cancel_and_missing_file_are_not_success(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Interrupted):
            validate_models(self.runtime, stop)
        (self.root / 'plugins/models/cugan' / cugan_model_name(-1)).unlink()
        with self.assertRaisesRegex(ValueError, 'error.denoise_model_missing'):
            validate_models(self.runtime, threading.Event())

    def test_model_failure_prevents_plugin_or_encoding_execution(self):
        with patch('borasuki.runtime.validation_signature', return_value={}), \
             patch('borasuki.runtime.ProcessGroup.capture') as capture:
            with self.assertRaisesRegex(ValueError, 'error.model_integrity'):
                validate_runtime(self.runtime, self.service.data, threading.Event(), lambda **fields: None)
        capture.assert_not_called()
