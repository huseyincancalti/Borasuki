"""Opt-in encoder comparison; retains samples and never changes a saved job."""

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

from borasuki.pipeline import render_segment, script_args, source_signature, verify_video
from borasuki.profile import segment_encode_args
from borasuki.process import ProcessError, ProcessGroup
from borasuki.storage import ROOT, atomic_json


def encoder_config(config, name):
    if name == 'x265':
        return {**config, 'encoding': dict(config['encoding'])}
    if name == 'nvenc':
        return {**config, 'encoding': {'codec': 'hevc_nvenc', 'preset': 'p7', 'cq': 15}}
    raise ValueError(f'Unknown encoder candidate: {name}')


def encode_args(config, source, output, name):
    args = segment_encode_args(encoder_config(config, name), config['runtime'], output)
    args[args.index('-i') + 1] = str(source)
    return args


def run_logged(group, args, log, timeout=100, **kwargs):
    with log.open('wb') as errors:
        child = group.spawn(args, stdout=subprocess.DEVNULL, stderr=errors, **kwargs)
        deadline = time.monotonic() + timeout
        while child.poll() is None:
            if group.stop.wait(0.1):
                raise ProcessError(f'Experiment cancelled; see {log}')
            if time.monotonic() > deadline:
                raise ProcessError(f'Experiment timeout; see {log}')
        if child.returncode:
            raise ProcessError(log.read_text(encoding='utf-8', errors='replace')[-6000:])


def produce(config, path, work, stop, start, count, output):
    log = work / (output.stem + '-inference.log')
    with log.open('wb') as errors, ProcessGroup(stop) as group:
        producer = group.spawn(script_args(path, config['runtime'], 'render', start, start + count,
                                           config.get('render_script')),
                               stdout=subprocess.PIPE, stderr=errors)
        args = [
            config['runtime']['ffmpeg'], '-hide_banner', '-nostdin', '-n', '-i', 'pipe:0',
            '-an', '-c:v', 'ffv1', '-level', '3', '-pix_fmt', 'yuv420p10le', str(output)]
        run_logged(group, args, work / (output.stem + '-encode.log'), stdin=producer.stdout)
        producer.stdout.close()
        if producer.wait(timeout=5):
            raise ProcessError(log.read_text(encoding='utf-8', errors='replace')[-6000:])
    if 'BORASUKI_STAGE=engine_building' in log.read_text(encoding='utf-8', errors='replace'):
        raise ProcessError('Unexpected engine build: cached comparison is invalid')


def quality(config, reference, output, work, stop):
    log = work / (output.stem + '-quality.log')
    graph = ('[0:v]format=yuv420p10le,setpts=PTS-STARTPTS,split[a][b];'
             '[1:v]format=yuv420p10le,setpts=PTS-STARTPTS,split[c][d];'
             '[a][c]ssim[s];[b][d]psnr[p]')
    with ProcessGroup(stop) as group:
        run_logged(group, [config['runtime']['ffmpeg'], '-hide_banner', '-nostdin',
                          '-i', str(output), '-i', str(reference), '-filter_complex', graph,
                          '-map', '[s]', '-map', '[p]', '-an', '-f', 'null', '-'], log)
    detail = log.read_text(encoding='utf-8', errors='replace')
    ssim = re.search(r'SSIM .*All:([\d.]+)', detail)
    psnr = re.search(r'PSNR .*average:([\d.]+)', detail)
    if not ssim or not psnr:
        raise ProcessError(f'Quality metrics missing: {log}')
    return {'ssim': float(ssim[1]), 'psnr_db': float(psnr[1]), 'bytes': output.stat().st_size}


def main(config_path, destination):
    config_path = Path(config_path)
    saved_config = config_path.read_bytes()
    config = json.loads(saved_config)
    signature = source_signature(Path(config['source']))
    work = Path(destination).resolve()
    work.mkdir(parents=True, exist_ok=False)
    if shutil.disk_usage(work).free < 3 * 1024 ** 3:
        raise ProcessError('At least 3 GiB free space is required for lossless samples')
    config.update(root=str(ROOT), work=str(work))
    path = work / 'config.json'
    atomic_json(path, config)
    stop = threading.Event()
    timer = threading.Timer(480, stop.set)
    timer.start()
    report = {'scope': 'Isolated experiment; production encoder unchanged', 'samples': [],
              'encoding': {name: encoder_config(config, name)['encoding'] for name in ('x265', 'nvenc')},
              'model': config.get('model'), 'tile': config['tile'], 'gpu_id': config['gpu_id']}
    try:
        with ProcessGroup(stop) as group:
            group.capture(script_args(path, config['runtime'], 'probe'), timeout=30)
        info = json.loads((work / 'source_info.json').read_text())
        fps = Fraction(info['fps_num'], info['fps_den'])
        count = 24
        if info['frames'] < count * 3:
            raise ValueError('At least 72 source frames required')
        starts = [0, (info['frames'] - count) // 2, info['frames'] - count]
        for index, start in enumerate(starts):
            reference = work / f'sample-{index}-reference.mkv'
            produce(config, path, work, stop, start, count, reference)
            row = {'source_start_frame': start, 'frames': count, 'encoders': {}}
            # Both encoders receive the same lossless inference output.
            for name in (('nvenc', 'x265') if index % 2 == 0 else ('x265', 'nvenc')):
                output = work / f'sample-{index}-{name}.mkv'
                started = time.monotonic()
                with ProcessGroup(stop) as group:
                    run_logged(group, encode_args(config, reference, output, name),
                               work / f'sample-{index}-{name}.log')
                seconds = time.monotonic() - started
                verified = verify_video(output, config['runtime'], stop, info['width'] * 2,
                                        info['height'] * 2, count, fps)
                video = next(s for s in verified['streams'] if s['codec_type'] == 'video')
                if video.get('pix_fmt') != 'yuv420p10le' or video.get('color_range') != 'tv':
                    raise ProcessError('Encoder did not preserve 10-bit limited-range output')
                row['encoders'][name] = {'encode_s': seconds,
                                          **quality(config, reference, output, work, stop)}
            report['samples'].append(row)
            atomic_json(work / 'report.json', report)
            print(json.dumps(row), flush=True)
        report['streamed_runs'] = []
        for index, name in enumerate(('nvenc', 'x265', 'x265', 'nvenc')):
            output = work / f'streamed-{index}-{name}.mkv'
            started = time.monotonic()
            atomic_json(path, encoder_config(config, name))
            render_segment(path, config['runtime'], output, starts[1], starts[1] + count,
                           stop, lambda frame: None, work / f'streamed-{index}-{name}.log')
            render_seconds = time.monotonic() - started
            verify_video(output, config['runtime'], stop, info['width'] * 2,
                         info['height'] * 2, count, fps)
            report['streamed_runs'].append({'encoder': name, 'render_s': render_seconds,
                                            'verified_s': time.monotonic() - started})
            atomic_json(work / 'report.json', report)
        assert source_signature(Path(config['source'])) == signature
        assert config_path.read_bytes() == saved_config
        report.update(passed=True, source_and_job_unchanged=True,
                      note='SSIM/PSNR measure encoder fidelity, not restoration quality or visual acceptance.')
        atomic_json(work / 'report.json', report)
        print(json.dumps(report), flush=True)
    finally:
        timer.cancel()


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
