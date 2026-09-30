"""Short previews rendered with the final job configuration."""

import base64
import json
import math
from fractions import Fraction
from pathlib import Path

from borasuki.pipeline import render_segment, script_args, source_signature, validate_timecodes, verify_video
from borasuki.process import Interrupted, ProcessGroup
from borasuki.storage import ROOT, atomic_json
from borasuki.wait_progress import WaitProgress


def validate_range(start, duration):
    one_frame = type(duration) is str and duration == 'frame'
    if (type(start) not in (int, float) or not math.isfinite(start) or start < 0
            or not one_frame and (type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 5)):
        raise ValueError("error.preview_range")


def render(job, start, duration, stop, update):
    validate_range(start, duration)
    work, runtime = Path(job['work']), job['runtime']
    config = work / 'config.json'
    atomic_json(config, {**job, 'root': str(ROOT)})
    tracker = WaitProgress(update)
    tracker.set('source_reading')
    with ProcessGroup(stop) as group:
        group.capture(script_args(config, runtime, 'probe'), timeout=1800)
    info = json.loads((work / 'source_info.json').read_text(encoding='utf-8'))
    fps = Fraction(info['fps_num'], info['fps_den'])
    validate_timecodes(work / 'source.timecodes.txt', info['frames'], fps, job.get('media', {}).get('duration'))
    first = math.floor(start * float(fps) + 1e-6)
    last = min(info['frames'], first + (1 if duration == 'frame' else max(1, round(duration * float(fps)))))
    if first >= last:
        raise ValueError('error.preview_range')
    count = last - first
    update(status='rendering', total_frames=count, frames=0)
    media = {}
    completed = 0
    def progress(frame):
        nonlocal completed
        completed = max(completed, frame)
        update(stage='rendering', frames=completed)
        tracker.set('preview_encoding') if completed >= count else tracker.set('preview_enhanced', completed, count)

    for name, mode, scale in [('enhanced', 'render', job['upscale']['scale']), ('original', 'original', 1)]:
        display = work / f'preview-{name}.mp4'
        tracker.set('preview_' + name, 0, count)
        original_frames = 0
        def original_progress(frame):
            nonlocal original_frames
            original_frames = max(original_frames, frame)
            tracker.set('preview_encoding') if original_frames >= count else tracker.set('preview_original', original_frames, count)
        render_segment(config, runtime, display, first, last, stop,
                       lambda frame: progress(frame) if mode == 'render' else original_progress(frame),
                       work / 'preview.log', mode=mode, display=True,
                       on_stage=lambda stage: update(stage=stage),
                       on_inference=lambda frame: progress(frame) if mode == 'render' else None)
        tracker.set('verification')
        verify_video(display, runtime, stop, info['width'] * scale, info['height'] * scale, count, fps)
        media[name] = str(display)
    if source_signature(Path(job['source'])) != job['source_signature']:
        raise ValueError('error.source_changed')
    if stop.is_set():
        raise Interrupted()
    return {'files': media, 'fps': float(fps), 'total_frames': count, 'start_frame': first,
            'width': info['width'], 'height': info['height']}


def media_urls(files):
    if sum(Path(path).stat().st_size for path in files.values()) > 32 * 1024 * 1024:
        raise ValueError('error.preview_size')
    return {key: 'data:video/mp4;base64,' + base64.b64encode(Path(path).read_bytes()).decode('ascii')
            for key, path in files.items()}
