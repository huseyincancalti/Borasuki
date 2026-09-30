"""The user's reference render profile; no subprocess execution."""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


def processing_settings(upscale=None, denoise=None) -> tuple[dict, dict]:
    upscale = {"scale": 2} if upscale is None else dict(upscale)
    denoise = {"enabled": True, "strength": 1} if denoise is None else dict(denoise)
    if set(upscale) != {"scale"} or type(upscale["scale"]) is not int or upscale["scale"] != 2:
        raise ValueError("error.invalid_settings")
    if (set(denoise) != {"enabled", "strength"} or type(denoise["enabled"]) is not bool
            or type(denoise["strength"]) is not int or denoise["strength"] not in (1, 2, 3)):
        raise ValueError("error.invalid_settings")
    return upscale, denoise


def cugan_options(config: dict) -> dict:
    upscale, denoise = processing_settings(config.get("upscale"), config.get("denoise"))
    return {"scale": upscale["scale"], "noise": denoise["strength"] if denoise["enabled"] else -1,
            "tilesize": list(config["tile"]), "version": 1}


def cugan_tile(width: int, height: int, free_vram: int) -> list[int]:
    if free_vram < 1500:
        return [240, 136]
    # Four tiles per axis, including CUGAN's 4-pixel overlap on each edge.
    if (width, height) == (1920, 1080):
        return [486, 276]
    if (width, height) == (1080, 1920):
        return [276, 486]
    return [480, 270]


def cugan_model_name(noise: int) -> str:
    if type(noise) is not int or noise not in (-1, 1, 2, 3):
        raise ValueError("error.invalid_settings")
    suffix = "no-denoise" if noise == -1 else f"denoise{noise}x"
    return f"up2x-latest-{suffix}.onnx"


def fast_encoding():
    return {'codec': 'hevc_nvenc', 'preset': 'p7', 'cq': 15}


@dataclass(frozen=True)
class ReferenceProfile:
    """Known starting parameters, not a hardware compatibility guarantee."""

    gpu_id: int = 0
    tile_width: int = 480
    tile_height: int = 270

    scale: ClassVar[int] = 2
    noise: ClassVar[int] = 1
    fp16: ClassVar[bool] = True
    contrast: ClassVar[float] = 1.03
    crf: ClassVar[int] = 15
    preset: ClassVar[str] = "slow"
    x265_params: ClassVar[str] = (
        "no-sao=1:aq-mode=3:qcomp=0.70:psy-rd=1.6:psy-rdoq=2.0:bframes=8"
    )

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("gpu_id", self.gpu_id, 0),
            ("tile_width", self.tile_width, 1),
            ("tile_height", self.tile_height, 1),
        ):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")

    def cugan_options(self) -> dict[str, object]:
        return {
            "noise": self.noise,
            "scale": self.scale,
            "tilesize": [self.tile_width, self.tile_height],
        }

    def tensorrt_options(self) -> dict[str, object]:
        return {"fp16": self.fp16, "device_id": self.gpu_id}


def validate_output_path(output: Path) -> None:
    """Validate Windows output naming without touching disk."""
    if not output.is_absolute():
        raise ValueError("Output path must be absolute")
    if output.suffix.lower() not in (".mkv", ".mp4"):
        raise ValueError("Output must be MKV or MP4")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved |= {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³"}
    for part in output.parts[1:]:
        if part in (".", "..") or part.endswith((" ", ".")):
            raise ValueError("Invalid Windows output path component")
        if any(ord(char) < 32 or char in '<>:"/\\|?*' for char in part):
            raise ValueError("Invalid character in Windows output path")
        if part.split(".", 1)[0].rstrip(" ").upper() in reserved:
            raise ValueError("Reserved Windows output name")


def validate_output_tracks(output: Path, media: dict, confirmed=False) -> None:
    if output.suffix.lower() != ".mp4":
        return
    tracks = media.get("track_codecs")
    compatible_audio = {"aac", "mp3", "ac3", "eac3", "alac"}
    if (tracks is None or len(tracks) != len(media.get("streams", [])) or any(
            track.get("type") == "audio" and track.get("codec") not in compatible_audio for track in tracks)):
        raise ValueError("error.mp4_tracks")
    if type(confirmed) is not bool or (any(track.get("type") in ("subtitle", "attachment") for track in tracks)
                                     and not confirmed):
        raise ValueError("error.mp4_confirm_required")


def build_encode_args(
    ffmpeg: Path, source: Path, output: Path, profile: ReferenceProfile
) -> list[str]:
    """Plan full-stream reference encoding; runner must probe and verify files."""
    if not ffmpeg.is_absolute() or not source.is_absolute():
        raise ValueError("Executable and source paths must be absolute")
    validate_output_path(output)
    if output.suffix.lower() != ".mkv":
        raise ValueError("Reference encoding requires MKV")
    if str(source.resolve()).casefold() == str(output.resolve()).casefold():
        raise ValueError("Output must not overwrite the source")
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-n",
        "-i", "pipe:0", "-i", str(source),
        "-map", "0:v:0", "-map", "1:a?", "-map", "1:s?",
        "-c:v", "libx265", "-preset", profile.preset,
        "-crf", str(profile.crf), "-pix_fmt", "yuv420p10le",
        "-x265-params", profile.x265_params,
        "-c:a", "copy", "-c:s", "copy",
        "-progress", "pipe:2", "-nostats", str(output),
    ]


def segment_encode_args(config: dict, runtime: dict, output: Path, display=False) -> list[str]:
    matrix = {1: "bt709", 5: "bt470bg", 6: "smpte170m", 7: "smpte240m"}[config["matrix"]]
    args = [runtime["ffmpeg"], "-hide_banner", "-nostdin", "-n", "-i", "pipe:0", "-an"]
    if display:
        return [*args, "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-colorspace", matrix, "-color_range", "tv", "-movflags", "+faststart",
                "-progress", "pipe:2", "-nostats", "-f", "mp4", str(output)]
    profile = ReferenceProfile()
    encoding = config.get("encoding", {"preset": profile.preset, "crf": profile.crf,
                                       "x265_params": profile.x265_params})
    codec = encoding.get("codec", "libx265")
    if codec == "libx265":
        options = ["-c:v", codec, "-preset", encoding["preset"], "-crf", str(encoding["crf"]),
                   "-pix_fmt", "yuv420p10le", "-x265-params", encoding["x265_params"]]
    elif codec == "hevc_nvenc":
        gpu, preset, cq = config.get("gpu_id"), encoding.get("preset"), encoding.get("cq")
        if (set(encoding) != {"codec", "preset", "cq"} or type(gpu) is not int or gpu < 0
                or preset not in ("p5", "p6", "p7") or type(cq) is not int or not 1 <= cq <= 51):
            raise ValueError("error.invalid_settings")
        options = ["-c:v", codec, "-gpu", str(gpu), "-preset", preset, "-tune", "hq",
                   "-profile:v", "main10", "-rc", "vbr", "-cq", str(cq), "-b:v", "0",
                   "-pix_fmt", "p010le", "-spatial-aq", "1", "-aq-strength", "8",
                   "-rc-lookahead", "16", "-bf", "3"]
    else:
        raise ValueError("error.invalid_settings")
    return [*args, *options, "-colorspace", matrix, "-color_range", "tv",
            "-progress", "pipe:2", "-nostats", "-f", "matroska", str(output)]
