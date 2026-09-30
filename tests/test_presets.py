"""Preset persistence and job snapshots must remain independent."""

import copy
import math
import unittest
from unittest.mock import patch

import test_queue
from borasuki import presets
from borasuki.pipeline import source_signature


class PresetTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp
    reload = test_queue.QueueTests.reload

    def values(self, mode='custom'):
        return {'upscale': {'scale': 2}, 'denoise': {'enabled': False, 'strength': 3},
                'color_mode': mode, 'custom': {'contrast': 1.02, 'brightness': 0.01, 'saturation': 1.01},
                'color_overrides': {'brightness': 0.01}}

    def create(self, preset, name='output'):
        values = preset['values']
        return self.service.create(str(self.source), str(self.root / (name + '.mkv')), values['color_mode'], 0,
                                   values.get('custom', {}), values['upscale'], values['denoise'], preset=preset['id'])

    def test_persistence_duplicate_confirmation_and_snapshot(self):
        preset = self.service.save_preset('Anime soft', self.values())
        self.assertEqual(self.reload().list_presets(), [preset])
        with self.assertRaisesRegex(ValueError, 'error.preset_duplicate'):
            self.service.save_preset(' ANIME SOFT ', self.values())
        identifier = self.create(preset)
        before = copy.deepcopy(self.service.jobs[identifier])
        with self.assertRaisesRegex(ValueError, 'error.confirmation'):
            self.service.delete_preset(preset['id'])
        self.assertEqual(self.service.list_presets(), [preset])
        self.service.delete_preset(preset['id'], True)
        self.assertEqual(self.service.jobs[identifier], before)
        self.assertEqual(before['configuration']['preset'], preset)
        self.assertEqual(self.reload().list_presets(), [])
        with self.assertRaisesRegex(ValueError, 'error.preset_changed'):
            self.create(preset, 'other')

    def test_validates_values_and_does_not_persist_paths_or_analysis(self):
        values = self.values('adaptive')
        saved = self.service.save_preset('Auto', values)
        self.assertNotIn('custom', saved['values'])
        self.assertEqual(saved['values']['color_overrides'], {'brightness': 0.01})
        for field in ('source', 'output', 'gpu_id', 'analysis', 'model'):
            with self.assertRaisesRegex(ValueError, 'error.invalid_settings'):
                self.service.save_preset(field, {**values, field: 'unwanted'})
        for name in ('', ' ' * 3, 'a' * 81, 'line\nbreak'):
            with self.assertRaisesRegex(ValueError, 'error.preset_name'):
                self.service.save_preset(name, values)
        for wrong in (math.nan, math.inf, True, 5):
            with self.assertRaisesRegex(ValueError, 'error.invalid_settings'):
                presets.settings({**values, 'color_overrides': {'brightness': wrong}})

    def test_changed_values_cannot_claim_original_preset(self):
        preset = self.service.save_preset('Preset', self.values())
        preset['values']['custom']['brightness'] = 0
        with self.assertRaisesRegex(ValueError, 'error.preset_changed'):
            self.create(preset)
        self.assertEqual(self.service.jobs, {})

    def test_failed_write_keeps_existing_presets_and_jobs(self):
        before = self.service.save_preset('First', self.values())
        with patch.object(self.service.store, 'save_preset', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.service.save_preset('Second', self.values())
        self.assertEqual(self.service.list_presets(), [before])

    def test_preview_snapshot_survives_preset_deletion_before_commit(self):
        preset = self.service.save_preset('Preview recipe', self.values())
        values = {**preset['values'], 'source': str(self.source), 'output': str(self.root / 'preview.mkv'),
                  'gpu_id': 0, 'preset': preset['id']}
        token = self.service.request_preview(values)
        with patch('borasuki.service.render_preview', return_value={'files': {}}):
            self.service._run_preview(self.service.preview_task)
        self.assertEqual(self.service.preview_task['status'], 'ready')
        snapshot = copy.deepcopy(self.service.preview_task['job']['configuration'])
        self.service.delete_preset(preset['id'], True)
        self.service.commit_preview(token)
        self.assertEqual(self.service.jobs[token]['configuration'], snapshot)
        self.assertEqual(snapshot['preset'], preset)

    def test_adaptive_snapshot_keeps_fresh_baseline_and_only_recipe_overrides(self):
        preset = self.service.save_preset('Auto', self.values('adaptive'))
        baseline = {'contrast': 1.015, 'brightness': 0, 'saturation': 1.009}
        with patch.object(self.service, '_resolve_adaptive', return_value={
            'source_signature': source_signature(self.source),
            'analysis': baseline, 'overrides': {'brightness': 0.01}}):
            identifier = self.service.create(str(self.source), str(self.root / 'auto.mkv'), 'adaptive', 0, {},
                preset['values']['upscale'], preset['values']['denoise'], {'token': 'validated'}, preset['id'])
        snapshot = self.service.jobs[identifier]['configuration']
        self.assertEqual(snapshot['analysis'], baseline)
        self.assertEqual(snapshot['grade'], {**baseline, 'brightness': 0.01})
        self.assertNotIn('analysis', snapshot['preset']['values'])
