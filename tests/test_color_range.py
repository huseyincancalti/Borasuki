import tempfile
import unittest
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from borasuki.errors import failure_info
from borasuki.enhance import vapoursynth_color_expressions
from borasuki.pipeline import validate_checkpoint_range
from borasuki.storage import ROOT, atomic_json


class ColorRangeTests(unittest.TestCase):
    def test_prepare_executes_production_script_without_source_range(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {'root':str(ROOT), 'runtime':{'plugins':folder}, 'work':folder,
                      'media':{'width':1920,'height':1080}, 'gpu_id':0, 'cache':folder,
                      'upscale':{'scale':2}, 'denoise':{'enabled':False,'strength':1},
                      'tile':[486,276], 'matrix':1, 'grade':{'contrast':1.0,'brightness':0.0,'saturation':1.0}}
            path = Path(folder) / 'config.json'
            atomic_json(path, config)
            core = MagicMock()
            vs = SimpleNamespace(core=core, RGBS=1, RGB=2, GRAY=3, YUV420P10=4)
            model = SimpleNamespace(Backend=Mock(), CUGAN=Mock(), trtexec=Mock())
            script = (ROOT / 'borasuki/render.vpy').read_text(encoding='utf-8')
            with patch.dict(sys.modules, {'vapoursynth':vs, 'vsmlrt':model}), \
                 patch('os.add_dll_directory'), patch.object(sys, 'path', list(sys.path)), \
                 patch('borasuki.source.open_source') as decode:
                exec(compile(script, 'render.vpy', 'exec'), {'job':str(path), 'mode':'prepare', 'start':'0', 'end':'1'})
            decode.assert_not_called()
            model.CUGAN.assert_called_once()
            core.resize.Bicubic.assert_called_once_with(model.CUGAN.return_value, format=4, matrix=1,
                                                       range_in_s='full', range_s='limited')
            core.resize.Bicubic.return_value.get_frame.assert_called_once_with(0)

    def test_render_uses_named_ranges_not_inverted_numeric_enums(self):
        script = (ROOT / 'borasuki/render.vpy').read_text(encoding='utf-8')
        self.assertIn("source_range = 'full' if config['range'] == 0 else 'limited'", script)
        self.assertIn("'range_in_s': source_range, 'range_s': 'limited'", script)
        self.assertIn("if clip.format.color_family != vs.YUV:", script)
        self.assertIn("range_in_s='full', range_s='limited'", script)
        self.assertIn("range_in_s=source_range, range_s='full'", script)
        self.assertNotIn('range=1', script)
        self.assertNotIn('range_in=config', script)

    def test_original_preview_matrix_only_for_rgb_source(self):
        script = (ROOT / 'borasuki/render.vpy').read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            atomic_json(path, {'root': str(ROOT), 'runtime': {'plugins': folder},
                               'work': folder, 'range': 1, 'matrix': 1})
            for family, expected_matrix in ((1, None), (2, 1)):
                with self.subTest(family=family):
                    core = MagicMock()
                    vs = SimpleNamespace(core=core, YUV=1, RGB=2, YUV420P10=3)
                    clip = MagicMock()
                    clip.format.color_family = family
                    with patch.dict(sys.modules, {'vapoursynth': vs}), \
                         patch('os.add_dll_directory'), patch.object(sys, 'path', list(sys.path)), \
                         patch('borasuki.source.open_source', return_value=(clip, 0)):
                        exec(compile(script, 'render.vpy', 'exec'),
                             {'job': str(path), 'mode': 'original', 'start': '0', 'end': '4'})
                    kwargs = core.resize.Bicubic.call_args.kwargs
                    self.assertEqual(kwargs['format'], vs.YUV420P10)
                    self.assertEqual(kwargs['range_in_s'], 'limited')
                    self.assertEqual(kwargs['range_s'], 'limited')
                    self.assertEqual(kwargs.get('matrix'), expected_matrix)

    def test_old_checkpoints_cannot_mix_with_corrected_pixel_range(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            segment = work / 'segment-0000000000-0000000240.mkv'
            segment.write_bytes(b'old checkpoint')
            with self.assertRaisesRegex(ValueError, 'error.render_format_changed'):
                validate_checkpoint_range({}, work)
            self.assertEqual(segment.read_bytes(), b'old checkpoint')
            validate_checkpoint_range({'output_range': 'limited'}, work)
            failure = failure_info(ValueError('error.render_format_changed'))
            self.assertFalse(failure['retryable'])

    def test_no_checkpoint_or_only_partial_can_render_with_corrected_range(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            validate_checkpoint_range({}, work)
            partial = work / 'segment-0000000000-0000000240.part.mkv'
            partial.write_bytes(b'partial')
            validate_checkpoint_range({}, work)
            self.assertTrue(partial.exists())
