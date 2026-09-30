import unittest
import subprocess
from unittest.mock import patch

from borasuki.process import ProcessError
from tests.integration_tensorrt_graph import frame_digests, measure_segments
from tests.integration_encoding import encoder_config, encode_args
from tests.integration_portrait_speed import Telemetry


class PerformanceMeasurementTests(unittest.TestCase):
    def test_telemetry_records_missing_tool_without_fake_zero_readings(self):
        telemetry = Telemetry(0)
        with patch('tests.integration_portrait_speed.shutil.which', return_value=None):
            telemetry.sample()
        self.assertEqual(telemetry.rows, [{'error': 'nvidia-smi unavailable'}])

    def test_telemetry_failure_is_bounded_and_does_not_hide_the_error(self):
        telemetry = Telemetry(2)
        with patch('tests.integration_portrait_speed.shutil.which', return_value='nvidia-smi'), \
                patch('tests.integration_portrait_speed.subprocess.run',
                      side_effect=subprocess.TimeoutExpired('nvidia-smi', 3)) as run:
            telemetry.sample()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs['timeout'], 3)
        self.assertEqual(run.call_args.args[0][1:3], ['-i', '2'])
        self.assertIn('error', telemetry.rows[0])

    def test_encoder_experiment_preserves_reference_settings(self):
        config = {'matrix': 1, 'runtime': {'ffmpeg': 'ffmpeg'},
                  'encoding': {'preset': 'slow', 'crf': 15, 'x265_params': 'no-sao=1'}}
        args = encode_args(config, 'in.mkv', 'out.mkv', 'x265')
        for pair in [('-c:v', 'libx265'), ('-preset', 'slow'), ('-crf', '15'),
                     ('-pix_fmt', 'yuv420p10le'), ('-x265-params', 'no-sao=1')]:
            self.assertIn(pair, list(zip(args, args[1:])))

    def test_hardware_candidate_is_explicit_gpu_and_10_bit(self):
        config = {'gpu_id': 2, 'matrix': 1, 'runtime': {'ffmpeg': 'ffmpeg'}}
        args = encode_args(config, 'input with spaces.mkv', 'out.mkv', 'nvenc')
        for pair in [('-gpu', '2'), ('-pix_fmt', 'p010le'), ('-profile:v', 'main10'),
                     ('-color_range', 'tv'), ('-colorspace', 'bt709')]:
            self.assertIn(pair, list(zip(args, args[1:])))
        self.assertIn('-n', args)
        self.assertNotIn('-y', args)
        self.assertNotIn('-r', args)
        self.assertIn('input with spaces.mkv', args)
        with self.assertRaises(ValueError):
            encoder_config(config, 'unknown')

    def test_frame_digest_ignores_segment_timestamp_reset(self):
        digest = 'a' * 32
        first = f'#format: frame checksums\n0, 0, 0, 1, 24, {digest}\n'.encode()
        second = f'0, 16, 16, 1, 24, {digest}\n'.encode()
        self.assertEqual(frame_digests(first, 1), frame_digests(second, 1))

    def test_invalid_frame_output_is_rejected(self):
        for data, count in [(b'', 1), (b'0, bad', 1),
                            (b'0, 0, 0, 1, 24, broken', 1),
                            (f'0, 0, 0, 1, 0, {"a" * 32}'.encode(), 1)]:
            with self.subTest(data=data), self.assertRaises(ProcessError):
                frame_digests(data, count)

    def test_comparison_covers_same_frames_in_reversed_order(self):
        calls = []

        def sample(start, end, label):
            calls.append((start, end))
            return 1 + (end - start) * 0.5, list(range(start, end)), ''

        report = measure_segments(sample, 4)
        self.assertEqual(calls, [(0, 2), (2, 4), (0, 4), (0, 4), (0, 2), (2, 4)])
        self.assertEqual([r['mode'] for r in report['runs']], ['split', 'single', 'single', 'split'])
        self.assertEqual([r['seconds'] for r in report['runs']], [4, 3, 3, 4])
        self.assertTrue(report['identical_raw_frames'])

    def test_changed_frame_order_is_rejected(self):
        def sample(start, end, label):
            frames = list(range(start, end))
            return 1, frames[::-1] if 'single' in label else frames, ''

        with self.assertRaisesRegex(ProcessError, 'content or ordering'):
            measure_segments(sample, 4)

    def test_missing_frames_are_rejected(self):
        with self.assertRaisesRegex(ProcessError, 'frame count'):
            measure_segments(lambda start, end, label: (1, [], ''), 4)

    def test_invalid_frame_budget_is_rejected(self):
        for count in (0, 1, 3, -2):
            with self.subTest(count=count), self.assertRaises(ValueError):
                measure_segments(lambda *_: self.fail('Must not start rendering'), count)


if __name__ == '__main__':
    unittest.main()
