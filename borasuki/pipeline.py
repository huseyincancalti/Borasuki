"""Segmented streaming render with verified checkpoints and atomic publication."""

import json
import io
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from fractions import Fraction
from pathlib import Path

from borasuki.process import Interrupted, ProcessError, ProcessGroup
from borasuki.errors import RenderDiagnostics
from borasuki.profile import segment_encode_args
from borasuki.progress import RenderEstimate
from borasuki.storage import ROOT, atomic_json

logger = logging.getLogger(__name__)


def probe(path: Path, runtime: dict, stop: threading.Event, count=False) -> dict:
    args = [runtime["ffprobe"], "-v", "error", "-show_streams", "-show_format", "-of", "json"]
    if count:
        args += ["-count_packets"]
    with ProcessGroup(stop) as group:
        return json.loads(group.capture([*args, str(path)], timeout=600 if count else 60))


def video_stream(info: dict) -> dict:
    videos = [s for s in info.get("streams", []) if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise ValueError("error.no_video")
    return videos[0]


def source_signature(source: Path) -> list:
    stat = source.stat()
    return [str(source.resolve()), stat.st_size, stat.st_mtime_ns]


def validate_timecodes(path: Path, frames: int, fps: Fraction, source_duration: float | None = None) -> int:
    values = [float(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.startswith("#")]
    if len(values) != frames or frames <= 0 or fps <= 0 or not all(math.isfinite(value) for value in values):
        raise ValueError("error.vfr")
    if any(right <= left for left, right in zip(values, values[1:])):
        raise ValueError("error.vfr")
    duration = 1000 / float(fps)
    tolerance = max(2, duration * 0.05)
    deviations = [value - values[0] - index * duration for index, value in enumerate(values)]
    irregular = [index for index, delta in enumerate(deviations) if abs(delta) > tolerance]
    if not irregular:
        return 0
    # Bounded cut-tail repair, not general VFR conversion.
    if (frames >= 30 and source_duration is not None and math.isfinite(source_duration)
            and abs(source_duration * 1000 - frames * duration) <= tolerance
            and irregular == list(range(irregular[0], frames)) and len(irregular) <= 2
            and all(1 <= round(deviations[index] / duration) <= 2
                    and abs(deviations[index] - round(deviations[index] / duration) * duration) <= tolerance
                    for index in irregular)):
        return len(irregular)
    error = ValueError("error.vfr")
    error.add_note(f"Frame timing mismatch: first frame={irregular[0]}, count={len(irregular)}, "
                   f"offset={deviations[irregular[0]]:.3f} ms; expected interval={duration:.3f} ms.")
    raise error


def inspect_source(source: Path, runtime: dict, stop: threading.Event) -> dict:
    info = probe(source, runtime, stop)
    video = video_stream(info)
    if video.get("color_transfer") in ("smpte2084", "arib-std-b67") or video.get("color_primaries") == "bt2020":
        raise ValueError("error.hdr")
    if video.get("field_order", "unknown") not in ("unknown", "progressive"):
        raise ValueError("error.interlaced")
    try:
        fps = Fraction(video.get("avg_frame_rate", "0/1"))
        nominal = Fraction(video.get("r_frame_rate", "0/1"))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("error.vfr") from exc
    if fps <= 0 or nominal <= 0 or abs(float(fps / nominal) - 1) > 0.002:
        raise ValueError("error.vfr")
    if abs(float(video.get("start_time", 0))) > 0.1:
        error = ValueError("error.timestamp")
        error.add_note(f"Video start: {video['start_time']} s; supported magnitude: <= 0.1 s.")
        raise error
    matrices = {"bt709": 1, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7}
    color = video.get("color_space", "unknown")
    if color not in (*matrices, "unknown", "unspecified"):
        raise ValueError("error.color")
    return {"width": video["width"], "height": video["height"], "fps": float(fps),
            "fps_num": nominal.numerator, "fps_den": nominal.denominator,
            "duration": float(video.get("duration", info.get("format", {}).get("duration", 0))),
            "matrix": matrices.get(color, 1), "range": 0 if video.get("color_range") == "pc" else 1,
            "assumed_color": color not in matrices,
            "streams": [s["codec_type"] for s in info["streams"] if s["codec_type"] in ("audio", "subtitle", "attachment")],
            "track_codecs": [{"type": s["codec_type"], "codec": s.get("codec_name")}
                             for s in info["streams"] if s["codec_type"] in ("audio", "subtitle", "attachment")],
            "video_index": video["index"]}


def script_args(config: Path, runtime: dict, mode: str, start=0, end=0,
                script_path: Path | None = None) -> list[str]:
    streaming = mode in ('render', 'original')
    return [runtime["vspipe"], *(['--progress'] if streaming else []), "--arg", f"job={config}", "--arg", f"mode={mode}",
            "--arg", f"start={start}", "--arg", f"end={end}", "--container", "y4m",
            str(script_path or ROOT / "borasuki/render.vpy"), "-" if streaming else "--",
            *([] if streaming else ["--info"])]


def progress_frame(line):
    if line.startswith("frame="):
        value = line.partition("=")[2].split()
        if value and value[0].isdecimal():
            return int(value[0])
    return None


def inference_frame(line):
    match = re.match(r'^Frame:\s*(\d+)/(\d+)(?:\s|$)', line)
    return min(int(match[1]), int(match[2])) if match else None


def render_segment(config_path: Path, runtime: dict, output: Path, start: int, end: int,
                   stop: threading.Event, on_progress, logfile: Path, mode="render", on_stage=None,
                   display=False, on_inference=None) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    args = segment_encode_args(config, runtime, output, display)
    diagnostics = RenderDiagnostics()
    activity = [time.monotonic()]
    progress_time = [None]
    started = time.monotonic()
    first_frame = [None]
    last_frame = [0]
    log_lock = threading.Lock()
    with logfile.open("ab") as log, ProcessGroup(stop) as group:
        producer = group.spawn(script_args(config_path, runtime, mode, start, end,
                                           config.get('render_script')),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        consumer = group.spawn(args, stdin=producer.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        producer.stdout.close()

        def drain(stream, progress):
            reader = io.TextIOWrapper(stream, encoding='utf-8', errors='replace', newline=None)
            try:
                # VSPipe uses CR progress lines, FFmpeg uses LF.
                for raw in reader:
                    activity[0] = time.monotonic()
                    line = raw.strip()
                    with log_lock:
                        log.write(raw.encode('utf-8'))
                        log.flush()
                        diagnostics.add(line)
                    frame = progress_frame(line) if progress else None
                    inferred = inference_frame(line) if not progress else None
                    if inferred is not None and on_inference:
                        with log_lock:
                            on_inference(inferred)
                    if not progress and line.startswith('BORASUKI_STAGE=') and on_stage:
                        stage = line.partition('=')[2]
                        if stage in ('engine_building', 'engine_loading'):
                            on_stage(stage)
                    if frame is not None:
                        progress_time[0] = time.monotonic()
                        if frame > 0 and first_frame[0] is None:
                            first_frame[0] = progress_time[0]
                            if on_stage:
                                on_stage('rendering')
                        last_frame[0] = max(last_frame[0], frame)
                        with log_lock:
                            on_progress(min(frame, end - start))
            except (OSError, ValueError):
                if not stop.is_set():
                    logger.exception("Progress reader failed")
            finally:
                reader.close()

        readers = [threading.Thread(target=drain, args=(producer.stderr, False), daemon=True),
                   threading.Thread(target=drain, args=(consumer.stderr, True), daemon=True)]
        for reader in readers:
            reader.start()
        while producer.poll() is None or consumer.poll() is None:
            if stop.wait(0.2):
                raise Interrupted()
            if consumer.poll() not in (None, 0):
                for reader in readers:
                    reader.join(timeout=1)
                with log_lock:
                    raise ProcessError(f"FFmpeg exit: {consumer.returncode}\n" + diagnostics.detail())
            now = time.monotonic()
            last = progress_time[0] or activity[0]
            if now - last > (300 if progress_time[0] else 1800):
                raise ProcessError("error.watchdog")
        for reader in readers:
            reader.join(timeout=2)
        if producer.returncode or consumer.returncode:
            with log_lock:
                raise ProcessError(f"VSPipe exit: {producer.returncode}; FFmpeg exit: {consumer.returncode}\n" + diagnostics.detail())
        logger.info('Render segment mode=%s range=%s:%s frames=%s startup_s=%.3f total_s=%.3f display=%s',
                    mode, start, end, last_frame[0], (first_frame[0] or time.monotonic()) - started,
                    time.monotonic() - started, display)


def verify_video(path: Path, runtime: dict, stop: threading.Event, width: int, height: int, frames: int, fps: Fraction) -> dict:
    info = probe(path, runtime, stop, count=True)
    video = video_stream(info)
    actual_fps = Fraction(video.get("avg_frame_rate", "0/1"))
    if (video["width"], video["height"]) != (width, height) or int(video.get("nb_read_packets", -1)) != frames or actual_fps != fps:
        raise ProcessError("error.verify")
    return info


def check_hardware_encoder(config: dict, work: Path, width: int, height: int,
                           fps: Fraction, stop: threading.Event) -> None:
    if config.get('encoding', {}).get('codec') != 'hevc_nvenc':
        return
    runtime = config['runtime']
    with tempfile.TemporaryDirectory(prefix='encoder-check-', dir=work) as folder:
        sample = Path(folder) / 'check.mkv'
        args = segment_encode_args(config, runtime, sample)
        index = args.index('-i')
        args[index:index + 2] = ['-f', 'lavfi', '-i', f'color=size={width}x{height}:rate={fps}']
        args[-1:-1] = ['-frames:v', '1']
        with ProcessGroup(stop) as group:
            group.capture(args, timeout=30)
        info = verify_video(sample, runtime, stop, width, height, 1, fps)
        video = video_stream(info)
        if (video.get('codec_name') != 'hevc' or video.get('pix_fmt') != 'yuv420p10le'
                or video.get('color_range') != 'tv'):
            raise ProcessError('error.hardware_encoder_unsupported')


def validate_checkpoint_range(info: dict, work: Path) -> None:
    if info.get('output_range') != 'limited' and any(
            '.part.' not in path.name for path in work.glob('segment-*.mkv')):
        raise ValueError('error.render_format_changed')


def mp4_timing_filter(runtime, fps, stop):
    if fps is None or fps <= 0:
        raise ValueError('MP4 mux requires the verified frame rate.')
    with ProcessGroup(stop) as group:
        help_text = group.capture([runtime['ffmpeg'], '-hide_banner', '-nostdin', '-h', 'bsf=setts'], timeout=15)
    options = set(re.findall(rb'^\s*-(\w+)\s', help_text, re.MULTILINE))
    if not {b'time_base', b'pts', b'dts', b'duration'} <= options:
        raise ProcessError('FFmpeg setts lacks required MP4 timing options.\n'
                           + help_text.decode('utf-8', 'replace')[-3000:])
    timebase = f'{fps.denominator}/{fps.numerator}'
    if b'prescale' in options:
        return f'setts=prescale=1:time_base={timebase}:pts=PTS:dts=DTS:duration=1'
    # Older setts evaluates timestamps in the input time base.
    return f'setts=time_base={timebase}:pts=round(PTS*TB/TB_OUT):dts=round(DTS*TB/TB_OUT):duration=1'


def mux_args(runtime, listing, source, publish, identifier, mp4, fps=None, *, timing=None):
    args = [runtime['ffmpeg'], '-v', 'error', '-nostdin', '-n', '-f', 'concat', '-safe', '1', '-i', str(listing),
            '-i', str(source), '-map', '0:v:0', '-map', '1:a?']
    if not mp4:
        args += ['-map', '1:s?', '-map', '1:t?']
    args += ['-map_metadata', '1', '-map_chapters', '1', '-metadata', f'borasuki_job={identifier}', '-c', 'copy']
    if mp4:
        if fps is None or fps <= 0:
            raise ValueError('MP4 mux requires the verified frame rate.')
        if timing is None:
            timing = mp4_timing_filter(runtime, fps, threading.Event())
        args += ['-bsf:v', timing, '-video_track_timescale', str(fps.numerator),
                 '-tag:v', 'hvc1', '-movflags', '+faststart+use_metadata_tags', '-f', 'mp4']
    return [*args, str(publish)]


def execute(job: dict, stop: threading.Event, update, segment_frames=240) -> None:
    source, output, work = Path(job["source"]), Path(job["output"]), Path(job["work"])
    runtime = job["runtime"]
    work.mkdir(parents=True, exist_ok=True)
    if source_signature(source) != job["source_signature"]:
        raise ValueError("error.source_changed")
    if output.exists():
        info_path = work / "source_info.json"
        tags = probe(output, runtime, stop).get("format", {}).get("tags", {})
        owner = next((value for key, value in tags.items() if key.lower() == "borasuki_job"), None)
        if owner != job["id"] or not info_path.exists():
            raise ValueError("error.output_exists")
        info = json.loads(info_path.read_text(encoding="utf-8"))
        verify_video(output, runtime, stop, info["width"] * 2, info["height"] * 2, info["frames"], Fraction(info["fps_num"], info["fps_den"]))
        update(status="completed", stage="completed", frames=info["frames"], total_frames=info["frames"], eta=0, speed=None)
        return
    update(stage="preparing", status="running")
    config_path = work / "config.json"
    config = {**job, "root": str(ROOT)}
    atomic_json(config_path, config)
    info_path = work / "source_info.json"
    if not info_path.exists():
        with ProcessGroup(stop) as group:
            group.capture(script_args(config_path, runtime, "probe"), timeout=1800)
    info = json.loads(info_path.read_text(encoding="utf-8"))
    validate_checkpoint_range(info, work)
    if info.get('output_range') != 'limited':
        info['output_range'] = 'limited'
        atomic_json(info_path, info)
    total = info["frames"]
    fps = Fraction(info["fps_num"], info["fps_den"])
    if total <= 0 or fps <= 0:
        raise ValueError("error.no_video")
    validate_timecodes(work / "source.timecodes.txt", total, fps, job["media"].get("duration"))
    if abs(float(fps) / job["media"]["fps"] - 1) > 0.002:
        raise ValueError("error.vfr")
    mp4 = output.suffix.lower() == '.mp4'
    timing = mp4_timing_filter(runtime, fps, stop) if mp4 else None
    check_hardware_encoder(config, work, info['width'] * 2, info['height'] * 2, fps, stop)
    if job["color_mode"] == "adaptive" and not job.get("configuration"):
        update(stage="analyzing")
        analysis_path = work / "analysis.json"
        if not analysis_path.exists():
            with ProcessGroup(stop) as group:
                group.capture(script_args(config_path, runtime, "analyze"), timeout=600)
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        config["grade"] = {key: analysis[key] for key in ("contrast", "brightness", "saturation")}
        update(analysis=analysis)
    update(total_frames=total, stage="rendering")
    segments = []
    width, height = info["width"] * 2, info["height"] * 2
    estimate = RenderEstimate()
    for start in range(0, total, segment_frames):
        if stop.is_set():
            raise Interrupted()
        end = min(start + segment_frames, total)
        segment = work / f"segment-{start:010d}-{end:010d}.mkv"
        segments.append(segment)
        if segment.exists():
            try:
                verify_video(segment, runtime, stop, width, height, end - start, fps)
                update(frames=end)
                continue
            except ProcessError:
                logger.warning("Invalid checkpoint segment %s; rendering again", segment)
                segment.unlink()
        if shutil.disk_usage(work).free < 256 * 1024 * 1024:
            raise ProcessError("error.disk_space")
        partial = segment.with_suffix(".part.mkv")
        for attempt in range(3):
            partial.unlink(missing_ok=True)
            atomic_json(config_path, config)
            estimate.begin(time.monotonic())
            encoded = [0]
            update(stage='engine_loading', speed=None, eta=None, eta_provisional=True)

            def progress(frame):
                encoded[0] = max(encoded[0], frame)
                speed, eta = estimate.observe(frame, time.monotonic(), total - start - frame,
                                               math.ceil((total - end) / segment_frames))
                if frame <= 0:
                    return
                if speed is None:
                    update(frames=start + frame, persist=False)
                    return
                update(frames=start + frame, speed=round(speed, 2) if speed is not None else None,
                       eta=eta, eta_provisional=not bool(estimate.cycles), persist=False)

            try:
                render_segment(config_path, runtime, partial, start, end, stop, progress, work / "render.log",
                               on_stage=lambda stage: update(stage=stage, persist=False))
                break
            except ProcessError as exc:
                if config.get('require_engine_prepared') or attempt == 2 or not any(token in str(exc).lower() for token in ("out of memory", "outofmemory", "cudaerrormemoryallocation")):
                    raise
                config["tile"] = [max(64, (value // 4) * 2) for value in config["tile"]]
                estimate = RenderEstimate()
                update(tile=config["tile"], warning="warning.oom")
        verify_video(partial, runtime, stop, width, height, end - start, fps)
        os.replace(partial, segment)
        if job.get('require_engine_prepared'):
            estimate.complete(end - start, time.monotonic())
            update(speed=round(estimate.speed, 2), eta=estimate.remaining(total - end, 0), eta_provisional=False)
        update(frames=end)
    if stop.is_set():
        raise Interrupted()
    update(stage="muxing", speed=None, eta=None)
    listing = work / "segments.ffconcat"
    # filenames are generated, never derived from user text
    lines = ['ffconcat version 1.0']
    for index, path in enumerate(segments):
        lines.append(f"file '{path.name}'")
        if mp4:
            count = min(segment_frames, total - index * segment_frames)
            lines.append(f'duration {float(count / fps):.12f}')
    listing.write_text('\n'.join(lines) + '\n', encoding='ascii')
    output.parent.mkdir(parents=True, exist_ok=True)
    publish = output.parent / f".borasuki-{job['id']}.partial{output.suffix.lower()}"
    publish.unlink(missing_ok=True)
    args = mux_args(runtime, listing, source, publish, job['id'], mp4, fps, timing=timing)
    with ProcessGroup(stop) as group:
        group.capture(args, timeout=1800)
    update(stage="verifying")
    final_info = verify_video(publish, runtime, stop, width, height, total, fps)
    actual_streams = [s["codec_type"] for s in final_info["streams"]]
    for kind in ("audio", "subtitle", "attachment"):
        expected = 0 if mp4 and kind in ("subtitle", "attachment") else job["media"]["streams"].count(kind)
        if actual_streams.count(kind) != expected:
            raise ProcessError("error.verify")
    if source_signature(source) != job["source_signature"]:
        raise ValueError("error.source_changed")
    if stop.is_set():
        raise Interrupted()
    # Windows rename fails if a destination already exists
    if output.exists():
        raise ValueError("error.output_exists")
    publish.rename(output)
    update(status="completed", stage="completed", frames=total, eta=0, speed=None)
    for segment in segments:
        try:
            segment.unlink(missing_ok=True)
        except OSError:
            logger.exception("Completed segment cleanup failed: %s", segment)
