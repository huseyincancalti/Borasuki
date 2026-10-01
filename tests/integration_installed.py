"""Opt-in GPU regression using installed files, not the source package.

Run with Python -I and an installed app folder, existing runtime and new work folder.
"""

import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def main(app_folder, runtime_folder, work_folder, engine_cache=None):
    app = Path(app_folder).resolve()
    runtime_folder = Path(runtime_folder).resolve(strict=True)
    work = Path(work_folder).resolve()
    work.mkdir(parents=True, exist_ok=False)
    data = work / 'app-data'
    data.mkdir()
    if engine_cache:
        cache = data / 'engine-cache'
        cache.mkdir()
        for pattern in ('*.engine', '*.engine.cache'):
            for path in Path(engine_cache).glob(pattern):
                shutil.copy2(path, cache / path.name)
    os.environ['BORASUKI_DATA_DIR'] = str(data)
    # Reuse downloaded binaries only; never load a user's settings or jobs.
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(data / 'runtime'), str(runtime_folder)],
                   check=True, capture_output=True)
    sys.path.insert(0, str(app / '_internal'))
    from borasuki.pipeline import probe, video_stream
    from borasuki.preview import render as render_preview
    from borasuki.runtime import discover
    from borasuki.service import Service
    from borasuki.storage import ROOT, atomic_json

    if ROOT.resolve() != app / '_internal':
        raise AssertionError('Test must import the installed package.')
    for name, module in tuple(sys.modules.items()):
        if name == 'borasuki' or name.startswith('borasuki.'):
            if not Path(module.__file__).resolve().is_relative_to(ROOT):
                raise AssertionError(f'{name} did not come from the installed app.')
    logging.basicConfig(level=logging.INFO, filename=work / 'test.log', encoding='utf-8')
    runtime = discover()
    assert runtime['ready'], runtime['missing']
    assert Path(runtime['vspipe']).is_relative_to(data / 'runtime')
    source = work / 'Türkçe input & test.mp4'
    output = work / 'Türkçe output.mp4'
    subprocess.run([runtime['ffmpeg'], '-v', 'error', '-n', '-f', 'lavfi', '-i',
                    'testsrc2=size=480x270:rate=24:duration=0.166667', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=0.166667', '-frames:v', '4', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                    '-colorspace', 'bt709', '-color_range', 'tv', '-c:a', 'aac', str(source)],
                   check=True, timeout=30)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    service = Service(data, start_worker=False)
    try:
        service.save_settings({'notifications': False})
        service._run_setup()
        assert service.setup['status'] == 'ready', service.setup
        print('Installed runtime validation passed.', flush=True)
        token = service.request_analysis(str(source))
        service.analysis_task['future'].result(timeout=60)
        assert service.analysis_task['status'] == 'ready', service.snapshot()['analysis']
        print('Installed Adaptive analysis passed.', flush=True)
        denoise = {'enabled': True, 'strength': 2}
        gpu_id = runtime['gpus'][0]['id']
        service.request_preparation(str(source), {'scale': 2}, denoise, gpu_id)
        service._run_preparation(service.engine_task)
        assert service.engine_task['status'] == 'ready', service.engine_task
        print('Installed denoise-2 TensorRT preparation passed.', flush=True)
        identifier = service.create(str(source), str(output), 'adaptive', gpu_id, {},
                                    upscale={'scale': 2}, denoise=denoise,
                                    adaptive={'token': token, 'profile': 'normal', 'overrides': {}})
        service.worker.start()
        service.wake.set()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            job = service.jobs[identifier].copy()
            if job['status'] in ('failed', 'completed'):
                break
            time.sleep(0.25)
        assert job['status'] == 'completed', {'status': job['status'], 'failure': job.get('failure')}
        info = probe(output, runtime, threading.Event(), count=True)
        video = video_stream(info)
        assert (video['width'], video['height'], int(video['nb_read_packets'])) == (960, 540, 4)
        assert video['avg_frame_rate'] == '24/1'
        assert video['pix_fmt'] == 'yuv420p10le'
        assert video['color_range'] == 'tv'
        assert sum(s['codec_type'] == 'audio' for s in info['streams']) == 1
        assert service.store.load()[0]['status'] == 'completed'
        subprocess.run([runtime['ffmpeg'], '-v', 'error', '-xerror', '-i', str(output),
                        '-f', 'null', '-'], check=True, capture_output=True, timeout=30)
        print('Installed Queue -> MP4, audio and full decode passed.', flush=True)
        preview = render_preview(job, 0, 'frame', threading.Event(), lambda **fields: None)
        assert preview['total_frames'] == 1
        for path in preview['files'].values():
            subprocess.run([runtime['ffmpeg'], '-v', 'error', '-xerror', '-i', path, '-f', 'null', '-'],
                           check=True, capture_output=True, timeout=30)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        report = {'passed': True, 'installed_package': True, 'source_checkout_imports': False,
                  'adaptive': True, 'denoise': 2, 'queue': 'completed', 'container': 'mp4',
                  'resolution': '960x540', 'frames': 4, 'fps': 24, 'audio': True,
                  'precision': '10-bit', 'full_decode': True, 'preview': 'one frame, both sides',
                  'source_unchanged': True}
        atomic_json(work / 'report.json', report)
        print('Installed original/enhanced Preview decode passed.', flush=True)
    finally:
        service.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('app_folder')
    parser.add_argument('runtime_folder')
    parser.add_argument('work_folder')
    parser.add_argument('--engine-cache', help='Reuse compiled engines only; settings and jobs are never copied.')
    args = parser.parse_args()
    main(args.app_folder, args.runtime_folder, args.work_folder, args.engine_cache)
