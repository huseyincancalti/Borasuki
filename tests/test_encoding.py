import copy
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from borasuki.errors import failure_info, RenderDiagnostics
from borasuki.pipeline import check_hardware_encoder, execute, mp4_timing_filter, mux_args, source_signature
from borasuki.process import ProcessError, Interrupted
from borasuki.profile import ReferenceProfile, segment_encode_args
from borasuki.storage import atomic_json


class SegmentEncodingTests(unittest.TestCase):
    def setUp(self):
        self.runtime = {'ffmpeg': 'encoder.exe'}
        self.output = Path('output with spaces.mkv')
        self.profile = ReferenceProfile()
        self.legacy = {'matrix': 1, 'gpu_id': 0, 'encoding': {
            'preset': 'slow', 'crf': 15, 'x265_params': self.profile.x265_params}}
        self.hardware = {'matrix': 1, 'gpu_id': 2, 'encoding': {
            'codec': 'hevc_nvenc', 'preset': 'p7', 'cq': 15}}

    def test_legacy_command_is_unchanged(self):
        expected = ['encoder.exe', '-hide_banner', '-nostdin', '-n', '-i', 'pipe:0', '-an',
                    '-c:v', 'libx265', '-preset', 'slow', '-crf', '15', '-pix_fmt', 'yuv420p10le',
                    '-x265-params', self.profile.x265_params, '-colorspace', 'bt709', '-color_range',
                    'tv', '-progress', 'pipe:2', '-nostats', '-f', 'matroska', str(self.output)]
        self.assertEqual(segment_encode_args(self.legacy, self.runtime, self.output), expected)
        self.assertEqual(segment_encode_args({'matrix': 1}, self.runtime, self.output), expected)

    def test_preview_remains_browser_compatible_for_both_encoders(self):
        expected = ['encoder.exe', '-hide_banner', '-nostdin', '-n', '-i', 'pipe:0', '-an',
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
                    '-colorspace', 'bt709', '-color_range', 'tv', '-movflags', '+faststart',
                    '-progress', 'pipe:2', '-nostats', '-f', 'mp4', str(self.output)]
        for config in (self.legacy, self.hardware):
            self.assertEqual(segment_encode_args(config, self.runtime, self.output, True), expected)

    def test_hardware_command_preserves_gpu_precision_and_snapshot(self):
        before = copy.deepcopy(self.hardware)
        args = segment_encode_args(self.hardware, self.runtime, self.output)
        pairs = list(zip(args, args[1:]))
        for pair in [('-c:v', 'hevc_nvenc'), ('-gpu', '2'), ('-profile:v', 'main10'),
                     ('-pix_fmt', 'p010le'), ('-cq', '15'), ('-color_range', 'tv')]:
            self.assertIn(pair, pairs)
        self.assertNotIn('-r', args)
        self.assertNotIn('-y', args)
        self.assertIn('-n', args)
        self.assertEqual(self.hardware, before)

    def test_final_mp4_mux_omits_only_confirmed_unsupported_tracks(self):
        timing = 'setts=prescale=1:time_base=1001/24000:pts=PTS:dts=DTS:duration=1'
        with patch('borasuki.pipeline.mp4_timing_filter', return_value=timing) as detect:
            args = mux_args(self.runtime, Path('segments.ffconcat'), Path('source.mkv'),
                            Path('output.partial.mp4'), 'a' * 32, True, Fraction(24000, 1001))
            detect.assert_called_once()
        self.assertIn(('-map', '1:a?'), list(zip(args, args[1:])))
        self.assertNotIn('1:s?', args)
        self.assertNotIn('1:t?', args)
        self.assertIn(('-c', 'copy'), list(zip(args, args[1:])))
        self.assertIn(('-f', 'mp4'), list(zip(args, args[1:])))
        self.assertIn(('-movflags', '+faststart+use_metadata_tags'), list(zip(args, args[1:])))
        self.assertIn(('-video_track_timescale', '24000'), list(zip(args, args[1:])))
        self.assertIn('setts=prescale=1:time_base=1001/24000:pts=PTS:dts=DTS:duration=1', args)
        self.assertEqual(args[-1], 'output.partial.mp4')
        mkv = mux_args(self.runtime, Path('segments.ffconcat'), Path('source.mkv'),
                       Path('output.partial.mkv'), 'a' * 32, False)
        self.assertIn('1:s?', mkv)
        self.assertIn('1:t?', mkv)
        self.assertNotIn('-bsf:v', mkv)

    def test_mp4_timing_detects_actual_runtime_capabilities(self):
        base_help = b' -time_base <rational>\n -pts <string>\n -dts <string>\n -duration <string>\n'
        stop = threading.Event()
        for modern in (False, True):
            with self.subTest(modern=modern), patch('borasuki.pipeline.ProcessGroup') as group:
                capture = group.return_value.__enter__.return_value.capture
                capture.return_value = base_help + (b' -prescale <boolean>\n' if modern else b'')
                timing = mp4_timing_filter(self.runtime, Fraction(24000, 1001), stop)
                group.assert_called_once_with(stop)
                capture.assert_called_once_with(['encoder.exe', '-hide_banner', '-nostdin', '-h', 'bsf=setts'], timeout=15)
                if modern:
                    self.assertEqual(timing, 'setts=prescale=1:time_base=1001/24000:pts=PTS:dts=DTS:duration=1')
                else:
                    self.assertEqual(timing, 'setts=time_base=1001/24000:pts=round(PTS*TB/TB_OUT):dts=round(DTS*TB/TB_OUT):duration=1')

    def test_mp4_timing_rejects_missing_options_even_if_help_exits_successfully(self):
        for help_text in (b"Unknown bit stream filter 'setts'", b' -time_base <rational>\n'):
            with patch('borasuki.pipeline.ProcessGroup') as group:
                group.return_value.__enter__.return_value.capture.return_value = help_text
                with self.assertRaisesRegex(ProcessError, 'lacks required MP4 timing options'):
                    mp4_timing_filter(self.runtime, Fraction(24), threading.Event())

    def test_mp4_timing_failure_and_cancel_are_not_swallowed(self):
        for failure in (ProcessError('FFmpeg unavailable'), Interrupted()):
            with patch('borasuki.pipeline.ProcessGroup') as group:
                group.return_value.__enter__.return_value.capture.side_effect = failure
                with self.assertRaises(type(failure)):
                    mp4_timing_filter(self.runtime, Fraction(24), threading.Event())

    def test_mux_reuses_preflight_timing_and_mkv_does_not_probe(self):
        with patch('borasuki.pipeline.mp4_timing_filter') as detect:
            args = mux_args(self.runtime, Path('segments'), Path('source'), Path('out.mp4'),
                            'test', True, Fraction(24), timing='verified-filter')
            self.assertIn('verified-filter', args)
            mux_args(self.runtime, Path('segments'), Path('source'), Path('out.mkv'), 'test', False)
            detect.assert_not_called()

    def test_missing_mp4_timing_fails_before_gpu_render(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work / 'source.mp4'
            source.write_bytes(b'test source')
            output = work / 'result.mp4'
            atomic_json(work / 'source_info.json', {'width': 64, 'height': 64, 'frames': 1,
                        'fps_num': 24, 'fps_den': 1, 'output_range': 'limited'})
            (work / 'source.timecodes.txt').write_text('# timecode format v2\n0\n')
            job = {'id': 'test', 'source': str(source), 'output': str(output), 'work': str(work),
                   'runtime': self.runtime, 'source_signature': source_signature(source),
                   'media': {'fps': 24, 'duration': 1 / 24}, 'color_mode': 'reference'}
            with patch('borasuki.pipeline.mp4_timing_filter', side_effect=ProcessError('unsupported setts')), \
                    patch('borasuki.pipeline.check_hardware_encoder') as hardware, \
                    patch('borasuki.pipeline.render_segment') as render:
                with self.assertRaisesRegex(ProcessError, 'unsupported setts'):
                    execute(job, threading.Event(), lambda **_: None)
                hardware.assert_not_called()
                render.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(list(work.glob('segment-*.mkv')), [])

    def test_mp4_mux_requires_verified_fps(self):
        with self.assertRaisesRegex(ValueError, 'verified frame rate'):
            mux_args(self.runtime, Path('segments.ffconcat'), Path('source.mkv'),
                     Path('output.mp4'), 'a' * 32, True)

    def test_invalid_hardware_profile_is_not_silently_changed_or_fallback(self):
        bad = [{'codec': 'unknown'}, {'preset': 'slow'}, {'cq': True}, {'cq': 0},
               {'cq': 52}, {'cq': 15.5}, {'cq': '15'}, {'extra_args': '-y'}]
        for change in bad:
            config = copy.deepcopy(self.hardware)
            config['encoding'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                segment_encode_args(config, self.runtime, self.output)

    def test_capability_check_is_only_for_explicit_hardware_jobs(self):
        with patch('borasuki.pipeline.ProcessGroup') as group:
            check_hardware_encoder(self.legacy, Path('unused'), 1920, 1080, Fraction(24), threading.Event())
            group.assert_not_called()

    def test_capability_check_uses_selected_gpu_actual_size_fps_and_precision(self):
        config = {**self.hardware, 'runtime': self.runtime}
        info = {'streams': [{'codec_type': 'video', 'codec_name': 'hevc', 'pix_fmt': 'yuv420p10le', 'color_range': 'tv'}]}
        with tempfile.TemporaryDirectory() as folder, patch('borasuki.pipeline.ProcessGroup') as group, \
                patch('borasuki.pipeline.verify_video', return_value=info) as verify:
            check_hardware_encoder(config, Path(folder), 3840, 2160, Fraction(24000, 1001), threading.Event())
            args = group.return_value.__enter__.return_value.capture.call_args.args[0]
            self.assertIn('color=size=3840x2160:rate=24000/1001', args)
            self.assertEqual(args[args.index('-gpu') + 1], '2')
            self.assertEqual(args[args.index('-frames:v') + 1], '1')
            self.assertEqual(verify.call_args.args[3:7], (3840, 2160, 1, Fraction(24000, 1001)))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_capability_failure_and_cancel_are_not_swallowed(self):
        config = {**self.hardware, 'runtime': self.runtime}
        for failure in (ProcessError('No NVENC capable devices found'), Interrupted()):
            with tempfile.TemporaryDirectory() as folder, patch('borasuki.pipeline.ProcessGroup') as group:
                group.return_value.__enter__.return_value.capture.side_effect = failure
                with self.assertRaises(type(failure)):
                    check_hardware_encoder(config, Path(folder), 1920, 1080, Fraction(24), threading.Event())
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_hardware_errors_keep_specific_recovery_even_after_pipe_failure(self):
        cases = [('Driver does not support the required nvenc API version', 'driver'),
                 ('Cannot load nvEncodeAPI64.dll', 'driver'),
                 ("Unknown encoder 'hevc_nvenc'", 'missing'),
                 ("Provided device doesn't support required NVENC features", 'unsupported')]
        for message, suffix in cases:
            diagnostic = RenderDiagnostics()
            diagnostic.add(message)
            for _ in range(100):
                diagnostic.add('frame=9')
            diagnostic.add('fwrite() call failed: broken pipe')
            result = failure_info(ProcessError(diagnostic.detail()))
            with self.subTest(message=message):
                self.assertEqual(result['code'], f'error.hardware_encoder_{suffix}')
                self.assertFalse(result['retryable'])
                self.assertEqual(result['recovery'], 'setup')
        result = failure_info(ProcessError('hevc_nvenc: OpenEncodeSessionEx failed: out of memory'))
        self.assertEqual(result['code'], 'error.gpu_memory')
        for gpu in (None, -1, True, '0'):
            config = {**self.hardware, 'gpu_id': gpu}
            with self.subTest(gpu=gpu), self.assertRaises(ValueError):
                segment_encode_args(config, self.runtime, self.output)


if __name__ == '__main__':
    unittest.main()
