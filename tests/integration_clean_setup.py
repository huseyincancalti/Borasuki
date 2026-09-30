"""Opt-in local check for an app-owned runtime on the current NVIDIA GPU."""

import argparse
import subprocess
import tempfile
import threading
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from borasuki.pipeline import render_segment, verify_video
from borasuki.runtime import discover
from borasuki.service import Service
from borasuki.storage import ROOT, atomic_json


def main(runtime_dir):
    root = Path(runtime_dir).resolve()
    runtime = {'plugins': str(root / 'plugins'),
               'vspipe': str(root / 'python/Lib/site-packages/vapoursynth/vspipe.exe'),
               'ffmpeg': str(root / 'ffmpeg.exe'), 'ffprobe': str(root / 'ffprobe.exe'),
               'gpus': discover()['gpus'], 'missing': [], 'ready': True}
    with tempfile.TemporaryDirectory(prefix='borasuki-clean-setup-') as folder:
        work = Path(folder)
        source = work / 'source.mp4'
        subprocess.run([runtime['ffmpeg'], '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=480x270:rate=24:duration=0.5', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                        '-colorspace', 'bt709', '-y', str(source)], check=True, timeout=30)
        with patch('borasuki.service.discover', return_value=runtime):
            service = Service(work / 'data', start_worker=False)
            try:
                service._run_setup()
                if service.setup['status'] != 'ready':
                    raise AssertionError(service.setup)
                service.request_preparation(str(source), {'scale': 2}, {'enabled': True, 'strength': 1},
                                            runtime['gpus'][0]['id'])
                service._run_preparation(service.engine_task)
                if service.engine_task['status'] != 'ready':
                    raise AssertionError(service.engine_task)
                job = service._prepare(str(source), str(work / 'result.mkv'), 'original',
                                       runtime['gpus'][0]['id'], {}, 'clean-setup',
                                       upscale={'scale': 2}, denoise={'enabled': True, 'strength': 1})
                config = work / 'job.json'
                atomic_json(config, {**job, 'root': str(ROOT)})
                segment = work / 'segment.mkv'
                render_segment(config, runtime, segment, 0, 4, threading.Event(),
                               lambda frame: None, work / 'render.log')
                verify_video(segment, runtime, threading.Event(), 960, 540, 4, Fraction(24, 1))
                print('App-owned runtime, GPU preparation and four-frame output passed')
            finally:
                service.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('runtime_dir')
    args = parser.parse_args()
    main(args.runtime_dir)
