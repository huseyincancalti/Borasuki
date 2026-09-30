"""Preview pipeline wiring, frame ranges and publication boundaries."""

import json
from fractions import Fraction
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from borasuki.pipeline import script_args, source_signature
from borasuki.preview import render, media_urls, validate_range


class PreviewPipelineTests(unittest.TestCase):
    def test_range_accepts_exact_frame_and_rejects_invalid_input(self):
        for duration in ('frame', 1, 2, 5, .5):
            validate_range(0, duration)
        for start, duration in ((-1, 'frame'), (True, 1), (float('nan'), 1), (0, True),
                                (0, float('inf')), (0, 0), (0, 6), (0, '1'), (0, None)):
            with self.subTest(start=start, duration=duration), self.assertRaisesRegex(ValueError, 'error.preview_range'):
                validate_range(start, duration)

    def test_one_frame_at_fractional_fps_is_aligned_and_never_modifies_source(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work / 'source.mp4'
            source.write_bytes(b'original source')
            signature = source_signature(source)
            fps = Fraction(24000, 1001)
            job = {'work':folder, 'source':str(source), 'source_signature':signature,
                   'output':str(work / 'final.mkv'), 'runtime':{'vspipe':'vspipe', 'ffmpeg':'ffmpeg'},
                   'upscale':{'scale':2}, 'grade':{'contrast':1, 'brightness':0, 'saturation':1}}
            job['media'] = {'duration':403 / float(fps)}
            (work / 'source_info.json').write_text(json.dumps({'frames':403, 'fps_num':fps.numerator,
                'fps_den':fps.denominator, 'width':160, 'height':90}))
            (work / 'source.timecodes.txt').write_text('\n'.join(str((i + max(0, i - 400)) * 1000 / float(fps)) for i in range(403)))
            def display(*args, **kwargs):
                Path(args[2]).write_bytes(b'mp4')
            with patch('borasuki.preview.ProcessGroup.capture'), patch('borasuki.preview.verify_video'), \
                 patch('borasuki.preview.render_segment', side_effect=display) as segment:
                for frame in (40, 320, 402):
                    result = render(job, frame / float(fps), 'frame', threading.Event(), lambda **fields: None)
                    self.assertEqual((result['start_frame'], result['total_frames']), (frame, 1))
                    for call in segment.call_args_list[-2:]:
                        self.assertEqual(call.args[3:5], (frame, frame + 1))
                with self.assertRaisesRegex(ValueError, 'error.preview_range'):
                    render(job, 403 / float(fps), 'frame', threading.Event(), lambda **fields: None)
            self.assertEqual(source_signature(source), signature)
            self.assertFalse(Path(job['output']).exists())

    def test_original_stream_is_not_an_inference_pass(self):
        args = script_args(Path('config.json'), {'vspipe':'vspipe'}, 'original', 24, 36)
        self.assertIn('mode=original', args)
        self.assertNotIn('--info', args)
        self.assertEqual(args[-1], '-')

    def test_range_clamps_to_end_and_uses_shared_renderer(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work / 'source.mkv'
            source.write_bytes(b'source')
            job = {'work':folder, 'source':str(source), 'source_signature':source_signature(source),
                   'output':str(work / 'final.mkv'), 'runtime':{'vspipe':'vspipe','ffmpeg':'ffmpeg'},
                   'upscale':{'scale':2}, 'grade':{'contrast':1.07,'brightness':.01,'saturation':1.02}}
            def capture(args, **kwargs):
                if args[0] == 'vspipe':
                    (work / 'source_info.json').write_text(json.dumps({'frames':36,'fps_num':24,'fps_den':1,'width':160,'height':90}))
                    (work / 'source.timecodes.txt').write_text('\n'.join(str(i*1000/24) for i in range(36)))
                else:
                    Path(args[-1]).write_bytes(b'mp4')
                return b''
            updates = []
            def render_display(*args, **kwargs):
                Path(args[2]).write_bytes(b'mp4')
            with patch('borasuki.preview.ProcessGroup.capture', side_effect=capture), \
                 patch('borasuki.preview.render_segment', side_effect=render_display) as segment, patch('borasuki.preview.verify_video') as verify:
                result = render(job, 1, 3, threading.Event(), lambda **fields: updates.append(fields))
            self.assertEqual(result['start_frame'], 24)
            self.assertEqual(result['total_frames'], 12)
            self.assertEqual([call.kwargs['mode'] for call in segment.call_args_list], ['render','original'])
            for call in segment.call_args_list:
                self.assertEqual(call.args[3:5], (24,36))
            self.assertTrue(all(call.kwargs['display'] for call in segment.call_args_list))
            self.assertEqual([call.args[3:5] for call in verify.call_args_list], [(320,180),(160,90)])
            self.assertEqual(json.loads((work / 'config.json').read_text())['grade'], job['grade'])
            self.assertFalse(Path(job['output']).exists())
            self.assertTrue(all(url.startswith('data:video/mp4;base64,') for url in media_urls(result['files']).values()))


if __name__ == '__main__':
    unittest.main()
