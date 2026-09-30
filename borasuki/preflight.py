"""Current source, output and disk checks; estimates are not guarantees."""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from borasuki.pipeline import source_signature
from borasuki.profile import cugan_options, cugan_model_name, validate_output_tracks
from borasuki.runtime import fingerprint

RESERVE = 256 * 1024 * 1024


def check(job: dict, runtime: dict, *, allow_published=False) -> dict:
    if not runtime["ready"]:
        raise ValueError("error.runtime_missing")
    if job["gpu_id"] not in [gpu["id"] for gpu in runtime["gpus"]]:
        raise ValueError("error.unsupported_gpu")
    noise = cugan_options(job)["noise"]
    model = Path(runtime["plugins"]) / "models/cugan" / cugan_model_name(noise)
    if not model.is_file():
        raise ValueError("error.denoise_model_missing")
    if fingerprint(runtime, job["gpu_id"], noise) != job["fingerprint"]:
        raise ValueError("error.runtime_changed")
    if job.get("model"):
        if hashlib.sha256(model.read_bytes()).hexdigest() != job["model"]["version"]:
            raise ValueError("error.runtime_changed")
    source, output, work = (Path(job[key]) for key in ("source", "output", "work"))
    validate_output_tracks(output, job['media'], job.get('drop_tracks_confirmed', False))
    try:
        if source_signature(source) != job["source_signature"]:
            raise ValueError("error.source_changed")
        with source.open("rb") as stream:
            stream.read(1)
    except OSError as exc:
        raise ValueError("error.source_access") from exc
    if output.exists() and not allow_published:
        raise ValueError("error.output_exists")
    # Encoded size varies with content; warn, never promise a precise size.
    estimate = max(RESERVE, job["source_signature"][1] * 4)
    volumes = {}
    for kind, folder in (("output", output.parent), ("temporary", work)):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".borasuki-preflight-", dir=folder)
            os.close(fd)
            Path(name).unlink()
            free = shutil.disk_usage(folder).free
            volume = folder.stat().st_dev
        except OSError as exc:
            raise ValueError("error.output_access" if kind == "output" else "error.temp_access") from exc
        entry = volumes.setdefault(volume, {"paths": [], "free": free, "estimated": 0})
        entry["paths"].append(str(folder))
        entry["free"] = min(entry["free"], free)
        entry["estimated"] += estimate
    if any(entry["free"] < RESERVE for entry in volumes.values()):
        raise ValueError("error.disk_space")
    return {"volumes": list(volumes.values()), "approximate": True,
            "warning": "warning.disk_estimate" if any(v["free"] < v["estimated"] + RESERVE for v in volumes.values()) else None}
