"""Discover an existing runtime without changing the system installation."""

import csv
import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import tempfile
from pathlib import Path

from borasuki.process import Interrupted, ProcessGroup
from borasuki.profile import cugan_model_name
from borasuki.storage import DATA, ROOT, atomic_json


REQUIRED_NOISE = (-1, 1, 2, 3)
CUGAN_SHA256 = {
    -1: '3587278c3a7e62bbcb00a23b4de291ff906f62a88856ee457cbe4211cc593574',
    1: '74ecabf4c1b3cbd1c2b03cc76116a9c25305b4560a5a085198a93206ac029ccb',
    2: '24b0faddfef109ebcc4a0871a2d142bf33a126916a481cd99732c98214e24181',
    3: '7bcb0510f9f989e681a51d39b7170883d3d5e75f1ec23ef5146e8ea50c3cc845',
}


def validate_models(runtime, stop):
    hashes = {}
    for noise in REQUIRED_NOISE:
        path = Path(runtime['plugins']) / 'models/cugan' / cugan_model_name(noise)
        digest = hashlib.sha256()
        try:
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    if stop.is_set():
                        raise Interrupted()
                    digest.update(chunk)
        except OSError as exc:
            error = ValueError('error.denoise_model_missing')
            error.add_note(f'Model: {path.name}; {exc}')
            raise error from exc
        hashes[path.name] = digest.hexdigest()
        if hashes[path.name] != CUGAN_SHA256[noise]:
            error = ValueError('error.model_integrity')
            error.add_note(f'Model: {path.name}; expected SHA-256: {CUGAN_SHA256[noise]}; actual: {hashes[path.name]}')
            raise error
    return hashes


def required_files(runtime):
    plugins = Path(runtime["plugins"])
    return {"VSPipe": Path(runtime["vspipe"]), "FFMS2": plugins / "ffms2.dll",
            "TensorRT": plugins / "vstrt.dll", "vsmlrt": plugins / "vsmlrt.py",
            "trtexec": plugins / "vsmlrt-cuda/trtexec.exe",
            **{cugan_model_name(noise): plugins / "models/cugan" / cugan_model_name(noise) for noise in REQUIRED_NOISE}}


def validation_signature(runtime):
    paths = [ROOT / "borasuki/runtime_check.vpy", *required_files(runtime).values()]
    paths += [Path(runtime[name]) for name in ("ffmpeg", "ffprobe") if runtime.get(name)]
    paths += sorted((Path(runtime["plugins"]) / "vsmlrt-cuda").glob("*.dll"))
    # VSPipe's adjacent Python extension and VapourSynth libraries.
    paths += sorted(Path(runtime["vspipe"]).parent.glob("*.dll"))
    paths += sorted(Path(runtime["vspipe"]).parent.glob("*.pyd"))
    return {"version": 2, "files": [[str(path), path.stat().st_size, path.stat().st_mtime_ns] for path in paths],
            "gpus": [[gpu["id"], gpu["name"], gpu["driver"]] for gpu in runtime["gpus"]]}


def validate_runtime(runtime, data, stop, update):
    if not runtime["ready"]:
        raise ValueError("error.runtime_missing")
    before = validation_signature(runtime)
    update(stage='models')
    model_hashes = validate_models(runtime, stop)
    with tempfile.TemporaryDirectory(prefix="runtime-check-", dir=data) as folder:
        work = Path(folder)
        config = work / "config.json"
        atomic_json(config, {"runtime": runtime, "report": str(work / "report.json")})
        update(stage="plugins")
        with ProcessGroup(stop) as group:
            group.capture([runtime["vspipe"], "--arg", f"config={config}", "--info",
                           str(ROOT / "borasuki/runtime_check.vpy"), "--"], timeout=60)
            group.capture([str(required_files(runtime)["trtexec"]), "--help"], timeout=30)
        report = json.loads((work / "report.json").read_text(encoding="utf-8"))
        report['models'] = model_hashes
        if not report.get("devices") or len(report["devices"]) != len(runtime["gpus"]):
            raise ValueError("error.unsupported_gpu")
        update(stage="encoding")
        sample = work / "check.mkv"
        with ProcessGroup(stop) as group:
            group.capture([runtime["ffmpeg"], "-v", "error", "-n", "-f", "lavfi", "-i", "color=size=32x32:rate=24",
                           "-frames:v", "1", "-c:v", "libx265", "-pix_fmt", "yuv420p10le", str(sample)], timeout=30)
            probe = json.loads(group.capture([runtime["ffprobe"], "-v", "error", "-show_streams", "-of", "json", str(sample)], timeout=15))
            if not any(s.get("codec_name") == "hevc" and s.get("pix_fmt") == "yuv420p10le" for s in probe["streams"]):
                raise ValueError("error.setup_encoding")
            encoders = group.capture([runtime["ffmpeg"], "-v", "error", "-encoders"], timeout=15)
            if b"libx264" not in encoders:
                raise ValueError("error.setup_encoding")
        if validation_signature(runtime) != before:
            raise ValueError("error.runtime_changed")
        return {"signature": before, "report": report}


def discover() -> dict:
    plugin_candidates = [DATA / "runtime" / "plugins", ROOT / "runtime" / "plugins",
                         Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "VapourSynth" / "plugins"]
    plugins = next((p for p in plugin_candidates if (p / "vstrt.dll").is_file()), plugin_candidates[0])
    pythons = [Path(sys.executable).parent]
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    pythons.extend(sorted(local.glob("Python*"), reverse=True))
    vspipes = [DATA / "runtime" / "python" / "Lib" / "site-packages" / "vapoursynth" / "vspipe.exe",
               ROOT / "runtime" / "vspipe.exe"]
    vspipes += [p / "Lib/site-packages/vapoursynth/vspipe.exe" for p in pythons]
    vspipe = next((p for p in vspipes if p.is_file()), vspipes[0])
    result = {"plugins": str(plugins), "vspipe": str(vspipe), "gpus": [], "missing": []}
    for name in ("ffmpeg", "ffprobe"):
        bundled = next((path for path in (DATA / "runtime" / f"{name}.exe", ROOT / "runtime" / f"{name}.exe")
                        if path.is_file()), None)
        result[name] = str(bundled) if bundled else shutil.which(name)
        if not result[name]:
            result["missing"].append(name)
    for name, path in required_files(result).items():
        if not path.is_file() or path.stat().st_size == 0:
            result["missing"].append(name)
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            with ProcessGroup(threading.Event()) as group:
                data = group.capture([smi, "--query-gpu=index,name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"], timeout=10)
            for row in csv.reader(data.decode("utf-8").splitlines()):
                result["gpus"].append(dict(id=int(row[0]), name=row[1].strip(), total=int(row[2]), free=int(row[3]), driver=row[4].strip()))
        except (OSError, RuntimeError, ValueError) as exc:
            result["gpu_error"] = str(exc)
    if not result["gpus"]:
        result["missing"].append("NVIDIA GPU")
    result["ready"] = not result["missing"]
    return result


def fingerprint(runtime: dict, gpu_id: int, noise=1) -> list:
    paths = [Path(runtime["vspipe"]), Path(runtime["plugins"]) / "vsmlrt.py",
             Path(runtime["plugins"]) / "vstrt.dll", Path(runtime["plugins"]) / "models/cugan" / cugan_model_name(noise)]
    gpu = next(g for g in runtime["gpus"] if g["id"] == gpu_id)
    return [gpu["name"], gpu["driver"], *[[str(p), p.stat().st_size, p.stat().st_mtime_ns] for p in paths]]


def seed_cache(runtime: dict, destination: Path) -> None:
    """Reuse local CUGAN engines; vsmlrt still selects its matching identity."""
    destination.mkdir(parents=True, exist_ok=True)
    model_folder = Path(runtime["plugins"]) / "models" / "cugan"
    for source in model_folder.glob("*.engine*"):
        if source.suffix not in (".engine", ".cache"):
            continue
        target = destination / source.name
        if not target.exists():
            shutil.copy2(source, target)


def gpu_memory() -> dict:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return {}
    try:
        with ProcessGroup(threading.Event()) as group:
            data = group.capture([smi, "--query-gpu=index,memory.used,memory.total", "--format=csv,noheader,nounits"], timeout=5)
        return {int(row[0]): {"used": int(row[1]), "total": int(row[2])} for row in csv.reader(data.decode("utf-8").splitlines())}
    except (OSError, RuntimeError, ValueError):
        logging.getLogger(__name__).debug("GPU memory measurement unavailable", exc_info=True)
        return {}
