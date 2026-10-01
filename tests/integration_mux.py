"""Opt-in MP4 timestamp regression; no GPU or user media required."""

import argparse
import json
import subprocess
import sys
import tempfile
import threading
from fractions import Fraction
from pathlib import Path


def main(runtime_folder, app_folder):
    sys.path.insert(0, str(Path(app_folder).resolve() / '_internal'))
    from borasuki.pipeline import mux_args, verify_video
    from borasuki.storage import ROOT
    assert ROOT == Path(app_folder).resolve() / '_internal'
    runtime = {name: str(Path(runtime_folder) / f'{name}.exe') for name in ('ffmpeg', 'ffprobe')}

    def run(*args):
        return subprocess.run(args, check=True, capture_output=True, timeout=30).stdout

    def hashes(path):
        result = run(runtime['ffmpeg'], '-v', 'error', '-xerror', '-i', str(path),
                     '-map', '0:v:0', '-f', 'framemd5', '-').decode()
        return [line.rsplit(',', 1)[1].strip() for line in result.splitlines() if not line.startswith('#')]

    for fps in (Fraction(24), Fraction(24000, 1001)):
        with tempfile.TemporaryDirectory(prefix='borasuki-mp4-timing-') as folder:
            work = Path(folder)
            expected = []
            lines = ['ffconcat version 1.0']
            for index, count in enumerate((7, 11)):
                segment = work / f'segment-{index}.mkv'
                run(runtime['ffmpeg'], '-v', 'error', '-n', '-f', 'lavfi', '-i',
                    f'testsrc2=size=96x64:rate={fps}', '-frames:v', str(count), '-c:v', 'libx265',
                    '-preset', 'ultrafast', '-pix_fmt', 'yuv420p10le',
                    '-x265-params', 'log-level=error:bframes=3', str(segment))
                expected.extend(hashes(segment))
                lines.extend((f"file '{segment.name}'", f'duration {float(count / fps):.12f}'))
            listing = work / 'segments.ffconcat'
            listing.write_text('\n'.join(lines) + '\n', encoding='ascii')
            audio = work / 'audio.m4a'
            run(runtime['ffmpeg'], '-v', 'error', '-n', '-f', 'lavfi', '-i',
                f'sine=duration={float(18 / fps):.12f}', '-c:a', 'aac', str(audio))
            output = work / 'result.mp4'
            run(*mux_args(runtime, listing, audio, output, 'timing-test', True, fps))
            info = verify_video(output, runtime, threading.Event(), 96, 64, 18, fps)
            assert sum(stream['codec_type'] == 'audio' for stream in info['streams']) == 1
            assert hashes(output) == expected, 'MP4 changed frame content or display order.'
            packets = json.loads(run(runtime['ffprobe'], '-v', 'error', '-select_streams', 'v',
                                     '-show_packets', '-of', 'json', str(output)))['packets']
            video = next(stream for stream in info['streams'] if stream['codec_type'] == 'video')
            timebase = Fraction(video['time_base'])
            assert sorted(Fraction(packet['pts']) * timebase for packet in packets) == [i / fps for i in range(18)]
            dts = [int(packet['dts']) for packet in packets]
            assert all(right > left for left, right in zip(dts, dts[1:]))
            print(f'MP4 {fps} FPS: two segments, 18 frames, B-frame order, audio and decoded hashes passed.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('runtime_folder')
    parser.add_argument('app_folder')
    args = parser.parse_args()
    main(args.runtime_folder, args.app_folder)
