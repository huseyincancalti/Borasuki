"""Bounded TensorRT experiments; never mutate the source or saved job."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from fractions import Fraction
from pathlib import Path

from borasuki.pipeline import render_segment, script_args, source_signature, verify_video
from borasuki.process import ProcessGroup, ProcessError
from borasuki.storage import ROOT, atomic_json


def frame_digests(data, count):
    rows = [line.split(',') for line in data.decode().splitlines()
            if line.strip() and not line.startswith('#')]
    if len(rows) != count or any(len(row) != 6 for row in rows):
        raise ProcessError('Unexpected frame count or invalid framemd5 output')
    result = []
    for row in rows:
        size, digest = row[-2].strip(), row[-1].strip()
        if not size.isdigit() or int(size) <= 0 or len(digest) != 32 or any(
                c not in '0123456789abcdef' for c in digest):
            raise ProcessError('Invalid frame digest')
        result.append((int(size), digest))
    return result


def raw_sample(path, config, work, stop, start, end, label, profile=False):
    log = work / f'{label}.log'
    started = time.monotonic()
    with log.open('wb') as diagnostics, ProcessGroup(stop) as group:
        args = script_args(path, config['runtime'], 'render', start, end, config.get('render_script'))
        if profile:
            args.insert(1, '--filter-time')
        producer = group.spawn(args, stdout=subprocess.PIPE, stderr=diagnostics)
        try:
            data = group.capture([config['runtime']['ffmpeg'], '-v', 'error', '-i', 'pipe:0',
                                  '-map', '0:v:0', '-c:v', 'rawvideo', '-f', 'framemd5', '-'],
                                 stdin=producer.stdout, timeout=65)
            producer.stdout.close()
            if producer.wait(timeout=5):
                raise ProcessError('VSPipe failed')
        except ProcessError as error:
            detail = log.read_text(encoding='utf-8', errors='replace')[-5000:]
            raise ProcessError(f'{label}: {error}\n{detail}') from error
    elapsed = time.monotonic() - started
    detail = log.read_text(encoding='utf-8', errors='replace')
    if 'BORASUKI_STAGE=engine_building' in detail:
        raise ProcessError('Engine build invalidates this cache-hit measurement')
    return elapsed, frame_digests(data, end - start), detail


def measure_segments(sample, count=32):
    if count < 2 or count % 2:
        raise ValueError('Segment comparison requires a positive even frame count')
    report, reference = [], None
    # Reverse order; never present a single thermal/order sample as a speedup.
    for repeat, order in enumerate((('split', 'single'), ('single', 'split'))):
        for name in order:
            ranges = [(0, count)] if name == 'single' else [(0, count // 2), (count // 2, count)]
            durations, frames = [], []
            for part, (start, end) in enumerate(ranges):
                elapsed, digests, _ = sample(start, end, f'segments-{repeat}-{name}-{part}')
                if len(digests) != end - start:
                    raise ProcessError('Segment frame count differs from requested range')
                durations.append(elapsed)
                frames.extend(digests)
            if reference is None:
                reference = frames
            elif frames != reference:
                raise ProcessError('Segmentation changed frame content or ordering')
            report.append({'repeat': repeat, 'mode': name, 'seconds': sum(durations),
                           'segment_seconds': durations})
    return {'variant': 'segments', 'frames_per_run': count, 'runs': report,
            'identical_raw_frames': True, 'engine_reused': True,
            'scope': 'decode+inference+conversion+pipe+hash; no final encoding',
            'note': 'Difference includes process startup, decode seek and pipeline drain; not engine load alone.'}


def main(config_path, variant='graph'):
    if variant not in {'graph', 'profile', 'streams', 'tiles', 'final-tiles', 'segments'}:
        raise ValueError(f'Unknown experiment: {variant}')
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    signature = source_signature(Path(config['source']))
    report, hashes = {}, []
    with tempfile.TemporaryDirectory(prefix='borasuki-trt-graph-') as folder:
        work = Path(folder)
        config.update(work=folder, root=str(ROOT))
        path = work / 'config.json'
        atomic_json(path, config)
        stop = threading.Event()
        timer = threading.Timer(600 if variant == 'tiles' else 180, stop.set)
        timer.start()
        try:
            with ProcessGroup(stop) as group:
                group.capture(script_args(path, config['runtime'], 'probe'), timeout=30)
            count = 12 if variant == 'profile' else 32
            baseline_tile = list(config['tile'])
            if variant == 'segments':
                info = json.loads((work / 'source_info.json').read_text())
                if info['frames'] < count:
                    raise ValueError('Segment comparison needs at least 32 source frames')
                report = measure_segments(lambda start, end, label: raw_sample(
                    path, config, work, stop, start, end, label), count)
                assert signature == source_signature(Path(config['source']))
                report.update(passed=True, source_unchanged=True, tile=config['tile'],
                              model=config.get('model'), gpu_id=config['gpu_id'])
                print(json.dumps(report))
                return
            if variant == 'final-tiles':
                info = json.loads((work / 'source_info.json').read_text())
                for label, tile in [('candidate', [486, 276]), ('baseline', baseline_tile)]:
                    config['tile'] = tile
                    atomic_json(path, config)
                    stages, encoded, inferred = [], [], []
                    output = work / f'{label}.mkv'
                    started = time.monotonic()
                    render_segment(path, config['runtime'], output, 0, count, stop,
                                   lambda frame: encoded.append([round(time.monotonic() - started, 3), frame]),
                                   work / f'{label}.log', on_stage=stages.append,
                                   on_inference=lambda frame: inferred.append([round(time.monotonic() - started, 3), frame]))
                    report[label + '_render_s'] = round(time.monotonic() - started, 3)
                    verify_video(output, config['runtime'], stop, info['width'] * 2, info['height'] * 2,
                                 count, Fraction(info['fps_num'], info['fps_den']))
                    report[label + '_verified_s'] = round(time.monotonic() - started, 3)
                    report[label + '_first_encoded_s'] = next(t for t, f in encoded if f > 0)
                    report[label + '_first_inference_s'] = next(t for t, f in inferred if f > 0)
                    assert 'engine_building' not in stages
                assert signature == source_signature(Path(config['source']))
                report.update(passed=True, variant=variant, frames=count, engine_reused=True,
                              speedup=round(report['baseline_verified_s'] / report['candidate_verified_s'], 3))
                print(json.dumps(report))
                return
            if variant == 'tiles':
                assert config['media']['width'] == 1920 and config['media']['height'] == 1080
                assert baseline_tile == [480, 270]
                config['tile'] = [486, 276]
                atomic_json(path, config)
                args = script_args(path, config['runtime'], 'render', 0, 1)
                args[-1] = os.devnull
                started = time.monotonic()
                with ProcessGroup(stop) as group:
                    group.capture(args, timeout=480)
                report['candidate_prepare_s'] = round(time.monotonic() - started, 3)
                print(json.dumps({'candidate_prepared': report['candidate_prepare_s']}), flush=True)
            for graph in ((False,) if variant == 'profile' else (False, True)):
                if variant == 'tiles':
                    config['tile'] = [486, 276] if graph else baseline_tile
                config['trt_cuda_graph'] = graph if variant == 'graph' else False
                config['trt_streams'] = 2 if variant == 'streams' and graph else 1
                atomic_json(path, config)
                log = work / f'graph-{graph}.log'
                elapsed, rows, detail = raw_sample(path, config, work, stop, 0, count,
                                                   f'graph-{graph}', profile=variant == 'profile')
                report['candidate_s' if graph else 'baseline_s'] = round(elapsed, 3)
                hashes.append(rows)
                if variant == 'profile':
                    report['filter_times'] = detail[-14000:]
                if variant == 'tiles':
                    with log.open('ab') as diagnostics, ProcessGroup(stop) as group:
                        producer = group.spawn(script_args(path, config['runtime'], 'render', 0, 4),
                                               stdout=subprocess.PIPE, stderr=diagnostics)
                        group.capture([config['runtime']['ffmpeg'], '-v', 'error', '-i', 'pipe:0',
                                       '-c:v', 'ffv1', str(work / f'quality-{graph}.mkv')],
                                      stdin=producer.stdout, timeout=45)
                        producer.stdout.close()
                        assert producer.wait(timeout=5) == 0
            assert signature == source_signature(Path(config['source']))
            if variant == 'profile':
                print(json.dumps(report))
                return
            if variant == 'tiles':
                with ProcessGroup(stop) as group:
                    data = group.capture([config['runtime']['ffmpeg'], '-v', 'error',
                                          '-i', str(work / 'quality-False.mkv'),
                                          '-i', str(work / 'quality-True.mkv'),
                                          '-lavfi', 'ssim,metadata=print:file=-', '-f', 'null', '-'], timeout=30)
                report['quality_ssim'] = data.decode()
                report['tiles'] = [baseline_tile, [486, 276]]
            else:
                assert hashes[0] == hashes[1], 'Runtime tuning changed decoded frame hashes'
            report.update(passed=True, variant=variant, frames=count, identical_raw_frames=hashes[0] == hashes[1], engine_reused=True,
                          speedup=round(report['baseline_s'] / report['candidate_s'], 3))
            print(json.dumps(report))
        finally:
            timer.cancel()


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'graph')
