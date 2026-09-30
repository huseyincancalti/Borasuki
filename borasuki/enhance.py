"""Conservative whole-video adjustment; fixed parameters prevent pumping."""

import math

import numpy as np


COLOR_LIMITS = {"contrast": (0.8, 1.2), "brightness": (-0.05, 0.05), "saturation": (0.8, 1.2)}
ADAPTIVE_PROFILES = ('normal', 'dark', 'high_saturation')


def validate_grade(values, *, partial=False):
    if not isinstance(values, dict) or set(values) - COLOR_LIMITS.keys() or not partial and set(values) != COLOR_LIMITS.keys():
        raise ValueError("error.invalid_settings")
    for key, value in values.items():
        minimum, maximum = COLOR_LIMITS[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError("error.invalid_settings")
    return dict(values)


def analyze(samples: list[np.ndarray], profile='normal') -> dict:
    if profile not in ADAPTIVE_PROFILES:
        raise ValueError('error.invalid_settings')
    identity = {"contrast": 1.0, "brightness": 0.0, "saturation": 1.0, "reason": "preserved", "version": 3, 'profile': profile}
    frames = []
    for sample in samples:
        frame = np.asarray(sample, dtype=np.float32)
        if frame.ndim not in (2, 3) or frame.shape[-1] != 3 or not frame.size or not np.isfinite(frame).all():
            raise ValueError("Invalid analysis samples: expected finite RGB pixels")
        frames.append(np.clip(frame.reshape(-1, 3), 0, 1))
    if len(samples) < 3:
        return identity
    weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    sampled_luma = [frame[::8] @ weights for frame in frames]
    pixels = np.concatenate([frame[::8] for frame in frames])
    luminance = pixels @ weights
    low, median, high = np.percentile(luminance, [1, 50, 99])
    dark_content = median < 0.18
    visible_samples = [sample for sample in sampled_luma if np.median(sample) > 0.05]
    shadow_share = float(np.mean([np.mean(sample < 0.18) for sample in visible_samples])) if visible_samples else 0.0
    shadow_protection = dark_content or shadow_share >= 0.12
    # Preserve intentional black, bright, and already high-contrast material.
    if median > 0.75 or high - low < 0.20 or high - low > 0.80 and not shadow_protection:
        return identity
    # Do not increase contrast when sampled near-whites could lose separation.
    highlight_protection = high >= 0.96
    contrast = 1.0 if shadow_protection or highlight_protection else 1.0 + min(0.03, max(0.0, (0.80 - (high - low)) * 0.10))
    chroma = float(np.mean(np.ptp(pixels, axis=1)))
    saturation = 1.0 + min(0.02, max(0.0, (0.15 - chroma) * 0.15))
    brightness = 0.0
    if profile == 'dark':
        brightness = -min(0.025, max(0.0, (median - 0.18) * 0.05))
        saturation = 1.0
    elif profile == 'high_saturation':
        saturation = 1.0 + (0.20 * max(0.0, 1.0 - chroma / 0.65) if chroma >= 0.015 else 0.0)
    elif shadow_protection:
        # Lift a dark video more; mixed-light videos get only a subtle lift.
        brightness = min(0.012, 0.004 + max(0.0, 0.18 - median) * 0.35) if dark_content else min(0.006, 0.003 + (shadow_share - 0.12) * 0.05)
        contrast = 1.0 - 2.0 * brightness
    # Check every pixel/channel in the small samples, not just the average.
    for strength in (1, .75, .5, .25):
        grade = {'contrast': round(float(1 + (contrast - 1) * strength), 6),
                 'brightness': round(float(brightness * strength), 6),
                 'saturation': round(float(1 + (saturation - 1) * strength), 6)}
        safe = True
        for frame in frames:
            proposed = transform(frame, **grade)
            for original, result in ((frame <= .001, proposed <= 0), (frame >= .999, proposed >= 1)):
                if np.any(np.mean(result, axis=0) > np.mean(original, axis=0) + .001):
                    safe = False
        if safe:
            changed = any(grade[key] != identity[key] for key in COLOR_LIMITS)
            return {**identity, **grade, 'reason': 'adjusted' if changed else 'preserved'}
    return identity


def analyze_profiles(samples):
    profiles = {name: analyze(samples, name) for name in ADAPTIVE_PROFILES}
    return {**profiles['normal'], 'profiles': profiles}


def select_profile(analysis, profile='normal'):
    if profile not in ADAPTIVE_PROFILES:
        raise ValueError('error.invalid_settings')
    if 'profiles' not in analysis:
        if profile != 'normal':
            raise ValueError('error.analysis_stale')
        return dict(analysis)
    return {**analysis, **analysis['profiles'][profile]}


def transform(rgb: np.ndarray, contrast: float, brightness: float, saturation: float) -> np.ndarray:
    luma = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    return ((rgb - luma[..., None]) * saturation + luma[..., None] - 0.5) * contrast + 0.5 + brightness


def vapoursynth_color_expressions(contrast: float, brightness: float, saturation: float) -> list[str]:
    luma = "x 0.2126 * y 0.7152 * + z 0.0722 * +"
    return [f"{plane} {luma} - {saturation} * {luma} + 0.5 - {contrast} * 0.5 + {brightness} +"
            for plane in ("x", "y", "z")]
