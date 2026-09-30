"""Reusable processing settings, never source-specific analysis or paths."""

from borasuki.enhance import ADAPTIVE_PROFILES, validate_grade
from borasuki.profile import processing_settings


def settings(values):
    if not isinstance(values, dict) or set(values) - {'upscale', 'denoise', 'color_mode', 'custom', 'color_overrides', 'adaptive_profile'}:
        raise ValueError('error.invalid_settings')
    if not {'upscale', 'denoise', 'color_mode'} <= values.keys():
        raise ValueError('error.invalid_settings')
    if not isinstance(values['upscale'], dict) or not isinstance(values['denoise'], dict):
        raise ValueError('error.invalid_settings')
    upscale, denoise = processing_settings(values['upscale'], values['denoise'])
    mode = values['color_mode']
    if mode not in ('original', 'reference', 'adaptive', 'custom'):
        raise ValueError('error.invalid_settings')
    result = {'upscale': upscale, 'denoise': denoise, 'color_mode': mode}
    if mode == 'custom':
        result['custom'] = validate_grade(values.get('custom'))
    elif mode == 'adaptive':
        profile = values.get('adaptive_profile', 'normal')
        if profile not in ADAPTIVE_PROFILES:
            raise ValueError('error.invalid_settings')
        result['adaptive_profile'] = profile
        result['color_overrides'] = validate_grade(values.get('color_overrides', {}), partial=True)
    return result


def from_job(job):
    return settings({key: job[key] for key in ('upscale', 'denoise', 'color_mode')} |
                    {'custom': job['grade'], 'color_overrides': job.get('color_overrides', {}),
                     'adaptive_profile': job.get('adaptive_profile', 'normal')})
