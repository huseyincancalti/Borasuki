"""Shared FFMS2 source policy for analysis, comparison and final render."""

import sys
from pathlib import Path
from fractions import Fraction

from borasuki.pipeline import validate_timecodes


def open_source(core, config):
    arguments = {"source": config["source"], "track": config["media"]["video_index"],
                 "cachefile": str(Path(config["work"]) / "source.ffindex"),
                 "timecodes": str(Path(config["work"]) / "source.timecodes.txt")}
    try:
        clip, mode = core.ffms2.Source(**arguments, seekmode=1), 1
    except Exception as exc:
        if "frame accurate seeking is not possible" not in str(exc).casefold():
            raise
        print("Borasuki: normal seeking failed; retrying with safe linear decoding (seekmode=0).", file=sys.stderr)
        clip, mode = core.ffms2.Source(**arguments, seekmode=0), 0
    media = config["media"]
    if media.get("fps_num") and media.get("fps_den"):
        fps = Fraction(media["fps_num"], media["fps_den"])
        repaired = validate_timecodes(Path(arguments["timecodes"]), clip.num_frames, fps, media.get("duration"))
        if repaired:
            print(f"Borasuki: normalized timing of {repaired} cut-tail frames at {fps}; "
                  "all decoded frames retained, source unchanged.", file=sys.stderr)
        if repaired or Fraction(clip.fps_num, clip.fps_den) != fps:
            clip = core.std.AssumeFPS(clip, fpsnum=fps.numerator, fpsden=fps.denominator)
    return clip, mode
