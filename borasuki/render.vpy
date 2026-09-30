import json
import os
import sys
from pathlib import Path

config = json.loads(Path(job).read_text(encoding="utf-8"))
sys.path.insert(0, config["root"])
from borasuki.storage import atomic_json
from borasuki.profile import cugan_options
from borasuki.source import open_source
from borasuki.enhance import vapoursynth_color_expressions

def wait_progress(stage, completed=None, total=None):
    if mode in ('prepare', 'analyze'):
        atomic_json(Path(config['work']) / 'wait-progress.json',
                    {'stage': stage, 'completed': completed, 'total': total})

wait_progress('engine_loading' if mode == 'prepare' else 'source_reading')

plugins = Path(config["runtime"]["plugins"])
dll_handles = [os.add_dll_directory(str(p)) for p in (plugins, plugins / "vsmlrt-cuda")]
sys.path.insert(0, str(plugins))
import vapoursynth as vs
from vapoursynth import core

core.std.LoadPlugin(str(plugins / "ffms2.dll"))
if mode == 'prepare':
    clip = core.std.BlankClip(width=config['media']['width'], height=config['media']['height'],
                              length=1, format=vs.RGBS)
else:
    clip, seek_mode = open_source(core, config)
    # Snapshot: 0=full, 1=limited; resize uses named ranges.
    source_range = 'full' if config['range'] == 0 else 'limited'
if mode == "probe":
    atomic_json(Path(config["work"]) / "source_info.json", {
        "frames": clip.num_frames, "fps_num": clip.fps_num, "fps_den": clip.fps_den,
        "width": clip.width, "height": clip.height,
        "seek_mode": seek_mode,
        "output_range": "limited",
    })
    clip.set_output()
elif mode == 'original':
    options = {'format': vs.YUV420P10, 'range_in_s': source_range, 'range_s': 'limited'}
    if clip.format.color_family != vs.YUV:
        options['matrix'] = config['matrix']
    original = core.resize.Bicubic(clip[int(start):int(end)], **options)
    original.set_output()
else:
    rgb = clip if mode == 'prepare' else core.resize.Bicubic(clip, format=vs.RGBS, matrix_in=config["matrix"],
                                                          range_in_s=source_range, range_s='full')
    if mode == "analyze":
        import numpy as np
        from borasuki.enhance import analyze_profiles
        small = core.resize.Bicubic(rgb, width=160, height=90)
        indices = sorted(set(round(i * (clip.num_frames - 1) / 8) for i in range(9)))
        samples = []
        wait_progress('color_sampling', 0, len(indices))
        for index in indices:
            with small.get_frame(index) as frame:
                samples.append(np.stack([np.asarray(frame[p]) for p in range(3)], axis=-1).copy())
            wait_progress('color_sampling', len(samples), len(indices))
        wait_progress('color_finishing')
        atomic_json(Path(config["work"]) / "analysis.json", {**analyze_profiles(samples), "seek_mode": seek_mode})
        clip.set_output()
    else:
        core.std.LoadPlugin(str(plugins / "vstrt.dll"))
        import vsmlrt
        import subprocess
        from types import SimpleNamespace
        build_log = Path(config["work"]) / "model-build.log"
        os.environ["TRTEXEC_LOG_FILE"] = str(build_log)
        def hidden_run(*args, **kwargs):
            if config.get('require_engine_prepared') and mode != 'prepare':
                raise RuntimeError('error.engine_preparation_required')
            print('BORASUKI_STAGE=engine_building', file=sys.stderr, flush=True)
            wait_progress('engine_building')
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            try:
                result = subprocess.run(*args, **kwargs)
                print('BORASUKI_STAGE=engine_loading', file=sys.stderr, flush=True)
                wait_progress('engine_loading')
                return result
            except subprocess.CalledProcessError:
                if build_log.exists():
                    print(build_log.read_text(encoding="utf-8", errors="replace")[-16000:], file=sys.stderr)
                raise
        vsmlrt.subprocess = SimpleNamespace(run=hidden_run)
        engines = []
        original_trtexec = vsmlrt.trtexec
        def tracked_trtexec(*args, **kwargs):
            engine = original_trtexec(*args, **kwargs)
            path = Path(engine).resolve()
            engines.append([str(path), path.stat().st_size, path.stat().st_mtime_ns])
            return engine
        vsmlrt.trtexec = tracked_trtexec
        grade = config["grade"]
        if grade != {"contrast": 1.0, "brightness": 0.0, "saturation": 1.0}:
            planes = [core.std.ShufflePlanes(rgb, p, vs.GRAY) for p in range(3)]
            expressions = vapoursynth_color_expressions(grade["contrast"], grade["brightness"], grade["saturation"])
            rgb = core.std.ShufflePlanes([core.std.Expr(planes, expr) for expr in expressions], [0, 0, 0], vs.RGB)
        rgb = rgb[int(start):int(end)]
        backend = vsmlrt.Backend.TRT(fp16=True, device_id=config["gpu_id"], engine_folder=config["cache"],
                                     use_cuda_graph=config.get('trt_cuda_graph', False),
                                     num_streams=config.get('trt_streams', 1))
        upscaled = vsmlrt.CUGAN(rgb, **cugan_options(config), backend=backend)
        final = core.resize.Bicubic(upscaled, format=vs.YUV420P10, matrix=config["matrix"],
                                   range_in_s='full', range_s='limited')
        if mode == 'prepare':
            wait_progress('engine_warming')
            with final.get_frame(0):
                pass
            atomic_json(Path(config['work']) / 'prepared-engines.json', engines)
        final.set_output()
