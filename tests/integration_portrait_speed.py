"""Same-source portrait tile experiment; no Queue or saved-job mutations."""

import json
import shutil
import subprocess
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

from borasuki import preparation
from borasuki.pipeline import render_segment, source_signature, verify_video
from borasuki.storage import ROOT, atomic_json
from tests.integration_encoding import produce, quality
from tests.integration_tensorrt_graph import raw_sample


class Telemetry:
    fields = ('utilization.gpu', 'utilization.memory', 'memory.used', 'power.draw',
              'temperature.gpu', 'clocks.sm', 'clocks.mem',
              'clocks_event_reasons.sw_power_cap', 'clocks_event_reasons.sw_thermal_slowdown')

    def __init__(self, gpu):
        self.gpu = gpu
        self.rows = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        started = time.monotonic()
        executable = shutil.which('nvidia-smi')
        if not executable:
            self.rows.append({'error': 'nvidia-smi unavailable'})
            return
        while not self.stop.is_set():
            try:
                data = subprocess.run(
                    [executable, '-i', str(self.gpu), '--query-gpu=' + ','.join(self.fields),
                     '--format=csv,noheader,nounits'], capture_output=True, text=True,
                    check=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
                self.rows.append({'seconds': round(time.monotonic() - started, 3),
                                  'values': data.stdout.strip()})
            except (OSError, subprocess.SubprocessError) as error:
                self.rows.append({'error': str(error)})
                return
            self.stop.wait(1)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=4)


def main(config_path, destination, candidate_tile=(276, 486), start_frame=240,
         candidate_cache=None, baseline_tile=(480, 270), candidate_model='cugan',
         candidate_script=None):
    config_path = Path(config_path)
    saved = config_path.read_bytes()
    original = json.loads(saved)
    assert (original['media']['width'], original['media']['height']) == (1080, 1920)
    assert (original['media']['fps_num'], original['media']['fps_den']) == (24, 1)
    assert original['tile'] == [480, 270]
    signature = source_signature(Path(original['source']))
    assert signature == original['source_signature'], 'Source changed since saved job'
    work = Path(destination).resolve()
    work.mkdir(parents=True, exist_ok=False)
    config = {**original, 'root': str(ROOT), 'work': str(work), 'require_engine_prepared': True}
    cache_by_variant = {'baseline': original['cache'], 'candidate': str(
        Path(candidate_cache).resolve() if candidate_cache else work / 'candidate-engine-cache')}
    Path(cache_by_variant['candidate']).mkdir(parents=True, exist_ok=True)
    path = work / 'config.json'
    stop = threading.Event()
    timer = threading.Timer(900, stop.set)
    report = {'source_signature': signature, 'model': config['model'], 'grade': config['grade'],
              'model_variants': {'baseline': 'Real-CUGAN denoise 2', 'candidate': candidate_model},
              'encoding': config['encoding'], 'start': start_frame, 'frames': 48, 'runs': [],
              'cache_isolated': True,
              'telemetry_fields': Telemetry.fields,
              'scope': 'Same source/settings, cached 48-frame render+verify; no audio/mux. '
                       'GPU telemetry is device-wide, not process-specific.'}
    variants = {'baseline': list(baseline_tile), 'candidate': list(candidate_tile)}
    candidate_script = Path(candidate_script).resolve() if candidate_script else work / f'render-{candidate_model}.vpy'
    if candidate_model in ('waifu2x', 'realesrgan_x2', 'trt_opt4', 'fast_resize'):
        source_script = ROOT / 'borasuki/render.vpy'
        script_text = source_script.read_text(encoding='utf-8')
        old_call = 'vsmlrt.CUGAN(rgb, **cugan_options(config), backend=backend)'
        if candidate_model == 'waifu2x':
            new_call = ('vsmlrt.Waifu2x(rgb, noise=3, scale=2, tilesize=tuple(config["tile"]), '
                        'model=vsmlrt.Waifu2xModel.anime_style_art_rgb, backend=backend)')
        elif candidate_model == 'realesrgan_x2':
            new_call = ('vsmlrt.RealESRGAN(rgb, tilesize=tuple(config["tile"]), '
                        'model=vsmlrt.RealESRGANv2Model.animevideo_xsx2, backend=backend)')
        elif candidate_model == 'fast_resize':
            new_call = 'core.resize.Bicubic(rgb, width=rgb.width * 2, height=rgb.height * 2)'
        else:
            new_call = old_call
            if candidate_model == 'trt_opt4':
                script_text = script_text.replace(
                    'vsmlrt.Backend.TRT(fp16=True',
                    'vsmlrt.Backend.TRT(fp16=True, builder_optimization_level=4')
        if old_call not in script_text:
            raise ValueError('Could not locate the production model call for isolated comparison')
        if candidate_model == 'trt_opt4' and 'builder_optimization_level=4' not in script_text:
            raise ValueError('Could not isolate TensorRT builder optimization level 4')
        if not candidate_script.is_file():
            candidate_script.write_text(script_text.replace(old_call, new_call), encoding='utf-8')
    if (len(variants['candidate']) != 2 or any(type(value) is not int or value <= 0 or value % 2
                                                for value in variants['candidate'])
            or variants['candidate'][0] > original['media']['width']
            or variants['candidate'][1] > original['media']['height']):
        raise ValueError('Candidate tile must be positive even dimensions within the source frame')
    report['tiles'] = variants
    timer.start()
    try:
        for label, tile in variants.items():
            config['tile'] = tile
            config['cache'] = cache_by_variant[label]
            if label == 'baseline':
                config.pop('render_script', None)
            elif candidate_model in ('waifu2x', 'realesrgan_x2', 'trt_opt4', 'fast_resize'):
                config['render_script'] = str(candidate_script)
            if label == 'candidate' and candidate_model == 'fast_resize':
                report[label + '_prepare_seconds'] = 0.0
                continue
            report['phase'] = 'prepare:' + label
            atomic_json(work / 'report.json', report)
            started = time.monotonic()
            print('Preparing ' + label, flush=True)
            preparation.prepare(config, work, stop)
            report[label + '_prepare_seconds'] = round(time.monotonic() - started, 3)
            atomic_json(work / 'report.json', report)
            print(json.dumps({label + '_prepare_seconds': report[label + '_prepare_seconds']}), flush=True)
        for index, label in enumerate(('baseline', 'candidate', 'candidate', 'baseline')):
            report['phase'] = f'render:{index}:{label}'
            atomic_json(work / 'report.json', report)
            config['tile'] = variants[label]
            config['cache'] = cache_by_variant[label]
            if label == 'baseline':
                config.pop('render_script', None)
            elif candidate_model in ('waifu2x', 'realesrgan_x2', 'trt_opt4', 'fast_resize'):
                config['render_script'] = str(candidate_script)
            config['require_engine_prepared'] = not (candidate_model == 'fast_resize' and label == 'candidate')
            atomic_json(path, config)
            output = work / f'{index}-{label}.mkv'
            stages = []
            with Telemetry(config['gpu_id']) as telemetry:
                started = time.monotonic()
                render_segment(path, config['runtime'], output, start_frame, start_frame + 48, stop,
                               lambda frame: None,
                               work / f'{index}-{label}.log', on_stage=stages.append)
                rendered = time.monotonic() - started
                info = verify_video(output, config['runtime'], stop, 2160, 3840, 48, Fraction(24, 1))
                elapsed = time.monotonic() - started
            video = next(s for s in info['streams'] if s['codec_type'] == 'video')
            assert video['pix_fmt'] == 'yuv420p10le' and video['color_range'] == 'tv'
            assert 'engine_building' not in stages
            row = {'variant': label, 'tile': variants[label], 'seconds': round(elapsed, 3),
                   'render_seconds': round(rendered, 3), 'telemetry': telemetry.rows}
            report['runs'].append(row)
            atomic_json(work / 'report.json', report)
            print(json.dumps({k: v for k, v in row.items() if k != 'telemetry'}), flush=True)
        # Filter times include parallel requests/waiting, not additive wall time.
        report['phase'] = 'profile-and-quality'
        atomic_json(work / 'report.json', report)
        config['tile'] = variants['candidate']
        config['cache'] = cache_by_variant['candidate']
        if candidate_model in ('waifu2x', 'realesrgan_x2', 'trt_opt4', 'fast_resize'):
            config['render_script'] = str(candidate_script)
        atomic_json(path, config)
        if candidate_model == 'fast_resize':
            report['scope'] = 'Pipeline ceiling only: no neural model, not a quality candidate.'
        else:
            elapsed, _, detail = raw_sample(path, config, work, stop, start_frame, start_frame + 12,
                                            'filter-profile', profile=True)
            report['profile'] = {'seconds': elapsed, 'detail': detail[-14000:]}
            for label, tile in variants.items():
                config['tile'] = tile
                config['cache'] = cache_by_variant[label]
                if label == 'baseline':
                    config.pop('render_script', None)
                elif candidate_model in ('waifu2x', 'realesrgan_x2', 'trt_opt4', 'fast_resize'):
                    config['render_script'] = str(candidate_script)
                atomic_json(path, config)
                produce(config, path, work, stop, start_frame, 4, work / f'{label}-lossless.mkv')
            report['tile_difference'] = quality(config, work / 'baseline-lossless.mkv',
                                                work / 'candidate-lossless.mkv', work, stop)
        assert config_path.read_bytes() == saved
        assert source_signature(Path(config['source'])) == signature
        report.update(passed=True, phase='complete', source_and_saved_job_unchanged=True)
        atomic_json(work / 'report.json', report)
        print(json.dumps({'passed': True, 'tile_difference': report.get('tile_difference')}), flush=True)
    except KeyboardInterrupt:
        stop.set()
        report.update(phase='cancelled', cancelled=True, source_and_saved_job_unchanged=
                      source_signature(Path(config['source'])) == signature and config_path.read_bytes() == saved)
        atomic_json(work / 'report.json', report)
        raise
    except Exception as error:
        report['error'] = str(error) or type(error).__name__
        report['budget_exhausted_or_cancelled'] = stop.is_set()
        atomic_json(work / 'report.json', report)
        raise
    finally:
        timer.cancel()


if __name__ == '__main__':
    if len(sys.argv) not in (3, 5, 6, 7):
        raise SystemExit('Usage: integration_portrait_speed.py <config.json> <new-folder> '
                         '[tile-width tile-height [start-frame [candidate-cache]]]')
    tile = tuple(map(int, sys.argv[3:])) if len(sys.argv) == 5 else (276, 486)
    if len(sys.argv) >= 6:
        tile = tuple(map(int, sys.argv[3:5]))
    start = int(sys.argv[5]) if len(sys.argv) >= 6 else 240
    cache = sys.argv[6] if len(sys.argv) == 7 else None
    main(sys.argv[1], sys.argv[2], tile, start, cache)
