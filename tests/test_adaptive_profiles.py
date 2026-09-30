"""Adaptive looks share source samples and freeze their selected grade."""

import copy
import threading
import unittest
from unittest.mock import patch

import numpy as np
import test_queue
from borasuki.enhance import ADAPTIVE_PROFILES, COLOR_LIMITS, analyze, analyze_profiles, transform, validate_grade
from borasuki.enhance import vapoursynth_color_expressions
from borasuki.pipeline import source_signature


def samples():
    gray = np.linspace(.2, .8, 2400).reshape(40, 60)
    return [np.stack((gray + .04, gray, gray - .04), axis=-1)] * 9


class ProfileColorTests(unittest.TestCase):
    def test_profiles_differ_in_requested_direction_and_normal_is_subtle(self):
        source = samples()
        profiles = analyze_profiles(source)['profiles']
        normal, dark, vivid = (profiles[name] for name in ADAPTIVE_PROFILES)
        self.assertEqual(normal['brightness'], 0)
        self.assertLessEqual(normal['contrast'], 1.03)
        self.assertLessEqual(normal['saturation'], 1.02)
        self.assertLess(dark['brightness'], 0)
        self.assertGreater(vivid['saturation'], normal['saturation'])
        for name, grade in profiles.items():
            validate_grade({key: grade[key] for key in COLOR_LIMITS})
            self.assertEqual(grade, analyze(source, name))

    def test_dark_flat_and_saturated_sources_are_not_blindly_pushed(self):
        for source in ([np.full((20, 20, 3), .03)] * 3,
                       [np.tile([.9, .1, .2], (20, 20, 1))] * 3):
            for profile in ADAPTIVE_PROFILES:
                self.assertEqual(analyze(source, profile)['reason'], 'preserved')
        with self.assertRaises(ValueError):
            analyze(samples(), 'unknown')

    def test_adaptive_lifts_dark_mixed_tones_without_increasing_contrast(self):
        gray = np.linspace(.03, .25, 2400, dtype=np.float32).reshape(40, 60)
        source = [np.repeat(gray[..., None], 3, axis=-1)] * 3
        grade = analyze(source, 'normal')
        self.assertGreater(grade['brightness'], 0)
        self.assertLessEqual(grade['brightness'], .012)
        self.assertLess(grade['contrast'], 1)
        result = transform(source[0], **{key: grade[key] for key in COLOR_LIMITS})
        self.assertGreater(float(result[..., 1].mean()), float(source[0][..., 1].mean()))
        self.assertLess(float(result.max()), 1)

    def test_highlight_heavy_low_contrast_video_does_not_get_extra_contrast(self):
        frame = np.full((40, 60, 3), .4, dtype=np.float32)
        frame[10:30, 10:50] = .98
        grade = analyze([frame] * 9)
        self.assertEqual(grade['contrast'], 1.0)

    def test_vapoursynth_expression_matches_reference_color_transform(self):
        grade = {'contrast': 1.03, 'brightness': .01, 'saturation': 1.02}
        frame = np.array([[.12, .2, .95], [.98, .97, .96]], dtype=np.float32)
        expected = transform(frame, **grade)
        expressions = vapoursynth_color_expressions(**grade)

        def evaluate(expression, pixel):
            stack = []
            for token in expression.split():
                if token in ('x', 'y', 'z'):
                    stack.append(float(pixel['xyz'.index(token)]))
                elif token in ('+', '-', '*'):
                    right, left = stack.pop(), stack.pop()
                    stack.append({'+': left + right, '-': left - right, '*': left * right}[token])
                else:
                    stack.append(float(token))
            self.assertEqual(len(stack), 1)
            return stack[0]

        actual = np.array([[evaluate(expr, pixel) for expr in expressions] for pixel in frame])
        np.testing.assert_allclose(actual, expected, atol=1e-6)

    def test_dark_adaptation_is_not_disabled_by_mixed_black_and_highlights(self):
        gray = np.linspace(.03, .25, 2400, dtype=np.float32).reshape(40, 60)
        frame = np.repeat(gray[..., None], 3, axis=-1)
        frame[0, 0] = 0
        frame[0, 1] = 1
        grade = analyze([frame] * 3, 'normal')
        self.assertGreater(grade['brightness'], 0)
        self.assertLess(grade['contrast'], 1)

    def test_mixed_video_uses_single_subtle_lift_when_many_sampled_shadows_exist(self):
        ramp = np.linspace(.08, .95, 2400, dtype=np.float32).reshape(40, 60)
        frame = np.repeat(ramp[..., None], 3, axis=-1)
        frame[::2, :20] = .06
        frame[::2, -1] = .99
        grade = analyze([frame] * 9, 'normal')
        self.assertGreater(grade['brightness'], 0)
        self.assertLessEqual(grade['brightness'], .006)
        self.assertLess(grade['contrast'], 1)

    def test_dark_lift_does_not_raise_near_black_or_change_bright_video(self):
        near_black = np.tile(np.linspace(0, .004, 1200, dtype=np.float32), 3).reshape(20, 60, 3)
        self.assertEqual(analyze([near_black] * 3, 'normal')['brightness'], 0)
        bright = np.linspace(.25, .95, 2400, dtype=np.float32).reshape(40, 60)
        bright = np.repeat(bright[..., None], 3, axis=-1)
        self.assertEqual(analyze([bright] * 3, 'normal')['brightness'], 0)

    def test_clipping_is_limited_per_sample_and_each_end_separately(self):
        source = samples()
        source[0] = np.random.default_rng(18).uniform(.001, .999, source[0].shape)
        for profile in ADAPTIVE_PROFILES:
            grade = analyze(source, profile)
            for frame in source:
                frame = frame.reshape(-1, 3)[::8]
                result = transform(frame, **{key: grade[key] for key in COLOR_LIMITS})
                self.assertLessEqual(np.mean(result <= 0), np.mean(frame <= .001) + .001)
                self.assertLessEqual(np.mean(result >= 1), np.mean(frame >= .999) + .001)

    def test_small_single_channel_highlights_and_shadows_are_checked(self):
        source = samples()
        source[0] = source[0].copy()
        pixels = source[0].reshape(-1, 3)
        # Off the statistics stride; a channel-specific loss must not be averaged away.
        pixels[[1, 9, 17, 25], 0] = .004
        pixels[[33, 41, 49, 57], 2] = .996
        for profile in ADAPTIVE_PROFILES:
            grade = analyze(source, profile)
            for frame in source:
                frame = frame.reshape(-1, 3)
                result = transform(frame, **{key: grade[key] for key in COLOR_LIMITS})
                for before, after in ((frame <= .001, result <= 0), (frame >= .999, result >= 1)):
                    self.assertTrue(np.all(np.mean(after, axis=0) <= np.mean(before, axis=0) + .001))

    def test_invalid_pixels_outside_statistics_stride_and_bad_shapes_rejected(self):
        for bad in (np.empty((0, 3)), np.zeros((4, 4)), np.zeros((3,))):
            with self.assertRaisesRegex(ValueError, 'Invalid analysis samples'):
                analyze([bad] * 3)
        for value in (np.nan, np.inf, -np.inf):
            frame = samples()[0].copy()
            frame.reshape(-1, 3)[1, 0] = value
            with self.assertRaisesRegex(ValueError, 'Invalid analysis samples'):
                analyze([frame] * 3)

    def test_neutral_ramp_keeps_equal_channels_and_inputs_are_not_modified(self):
        gray = np.linspace(.2, .8, 2400).reshape(40, 60)
        frame = np.repeat(gray[..., None], 3, axis=-1)
        original = frame.copy()
        grade = analyze([frame] * 3)
        result = transform(frame, **{key: grade[key] for key in COLOR_LIMITS})
        np.testing.assert_array_equal(frame, original)
        np.testing.assert_array_equal(result[..., 0], result[..., 1])
        np.testing.assert_array_equal(result[..., 1], result[..., 2])


class ProfileSnapshotTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp

    def test_one_analysis_supports_three_profiles_without_rereading_source(self):
        result = analyze_profiles(samples())
        with patch.object(self.service, '_analyze_config', return_value=result) as analysis:
            token = self.service.request_analysis(str(self.source))
            self.service.analysis_task['future'].result(timeout=3)
            for name in ADAPTIVE_PROFILES:
                identifier = self.service.create(str(self.source), str(self.root / (name + '.mkv')), 'adaptive', 0, {},
                    {'scale': 2}, {'enabled': False, 'strength': 3}, {'token': token, 'profile': name})
                job = self.service.jobs[identifier]
                self.assertEqual(job['adaptive_profile'], name)
                self.assertEqual(job['grade'], {key: result['profiles'][name][key] for key in COLOR_LIMITS})
                self.assertEqual(job['configuration']['adaptive_profile'], name)
                self.assertEqual(job['model']['noise'], -1)
            self.assertEqual(analysis.call_count, 1)

    def test_preview_preset_and_edit_preserve_profile_and_overrides(self):
        result = analyze_profiles(samples())
        with patch.object(self.service, '_analyze_config', return_value=result):
            token = self.service.request_analysis(str(self.source))
            self.service.analysis_task['future'].result(timeout=3)
        recipe = {'upscale': {'scale': 2}, 'denoise': {'enabled': False, 'strength': 3},
                  'color_mode': 'adaptive', 'adaptive_profile': 'dark', 'color_overrides': {'contrast': 1.01}}
        preset = self.service.save_preset('Cinema', recipe)
        values = {**recipe, 'source': str(self.source), 'output': str(self.root / 'preview.mkv'), 'custom': {},
                  'gpu_id': 0, 'preset': preset['id'], 'adaptive': {'token': token, 'profile': 'dark', 'overrides': {'contrast': 1.01}}}
        identifier = self.service.request_preview(values)
        self.service.cancel_analysis(token)
        with patch('borasuki.service.render_preview', return_value={'files': {}}):
            self.service._run_preview(self.service.preview_task)
        before = copy.deepcopy(self.service.preview_task['job']['configuration'])
        self.service.commit_preview(identifier)
        self.assertEqual(self.service.jobs[identifier]['configuration'], before)
        self.assertEqual(before['adaptive_profile'], 'dark')
        self.assertEqual(before['grade']['contrast'], 1.01)
        self.assertEqual(before['preset']['values'], recipe)
        edited = self.service.begin_edit(identifier)
        with patch.object(self.service, '_analyze_config', side_effect=AssertionError('Reanalysis')):
            self.service.request_analysis(str(self.source), {'id': identifier, 'token': edited['edit_token']})
        self.assertEqual(self.service.analysis_task['analysis']['profile'], 'dark')

    def test_legacy_analysis_stays_normal_and_cannot_fake_a_new_profile(self):
        legacy = {'contrast': 1.02, 'brightness': 0, 'saturation': 1.01, 'version': 1}
        self.service.analysis_task = {'id': 'old', 'source_signature': source_signature(self.source),
                                      'status': 'ready', 'analysis': legacy, 'stop': threading.Event()}
        self.assertEqual(self.service._resolve_adaptive(str(self.source), {'token': 'old'})['analysis'], legacy)
        with self.assertRaisesRegex(ValueError, 'error.analysis_stale'):
            self.service._resolve_adaptive(str(self.source), {'token': 'old', 'profile': 'dark'})
