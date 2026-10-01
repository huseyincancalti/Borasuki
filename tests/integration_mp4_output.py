"""CPU-only final MP4 mux check; no model or GPU build."""

import json
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

from borasuki.pipeline import mux_args


ffmpeg = shutil.which('ffmpeg')
ffprobe = shutil.which('ffprobe')
if not ffmpeg or not ffprobe:
    raise SystemExit('FFmpeg/FFprobe unavailable')

with tempfile.TemporaryDirectory(prefix='borasuki-mp4-') as folder:
    work = Path(folder)
    segment = work / 'segment-0000000000-0000000012.mkv'
    source = work / 'source.mkv'
    output = work / 'output.partial.mp4'
    subtitle = work / 'captions.srt'
    subtitle.write_text('1\n00:00:00,000 --> 00:00:00,400\nTest subtitle\n', encoding='utf-8')
    subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i',
                    'color=c=blue:s=64x64:r=24:d=0.5', '-frames:v', '12', '-c:v', 'libx265',
                    '-preset', 'ultrafast', '-x265-params', 'log-level=error:pools=1',
                    '-pix_fmt', 'yuv420p10le', '-an', str(segment)], check=True, timeout=40)
    subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i',
                    'color=c=blue:s=64x64:r=24:d=0.5', '-f', 'lavfi', '-i',
                    'sine=frequency=1000:sample_rate=48000:duration=0.5', '-f', 'srt', '-i',
                    str(subtitle), '-map', '0:v:0', '-map', '1:a:0', '-map', '2:s:0', '-c:v', 'libx264',
                    '-preset', 'ultrafast', '-c:a', 'aac', '-b:a', '64k', '-c:s', 'ass',
                    '-t', '0.5', str(source)], check=True, timeout=40)
    listing = work / 'segments.ffconcat'
    listing.write_text(f"ffconcat version 1.0\nfile '{segment.name}'\n", encoding='ascii')
    subprocess.run(mux_args({'ffmpeg': ffmpeg}, listing, source, output, 'a' * 32, True, Fraction(24)),
                   check=True, timeout=40)
    result = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-show_format',
                             '-of', 'json', str(output)], check=True, capture_output=True, timeout=20)
    info = json.loads(result.stdout)
    types = [stream['codec_type'] for stream in info['streams']]
    assert types == ['video', 'audio'], types
    assert info['streams'][0]['codec_name'] == 'hevc'
    assert info['streams'][0]['codec_tag_string'] == 'hvc1'
    assert info['streams'][1]['codec_name'] == 'aac'
    assert info['format']['tags']['borasuki_job'] == 'a' * 32
    print('MP4 mux passed: HEVC Main10 + AAC; subtitle omitted only in output; owner tag preserved')
