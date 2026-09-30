"""User-facing failure classification; keep the decoder evidence available."""

from collections import deque
import re


class RenderDiagnostics:
    def __init__(self):
        self.tail = deque(maxlen=20)
        self.causes = deque(maxlen=12)

    def add(self, line):
        self.tail.append(line)
        if any(marker in line.casefold() for marker in ('error', 'failed', 'exception', 'traceback',
                'permission denied', 'access is denied', 'no space left', 'disk full', 'out of memory', 'outofmemory',
                'nvenc', 'nvencodeapi')):
            self.causes.append(line)

    def detail(self):
        return '\n'.join([*self.tail, *self.causes])[-6000:]


def failure_info(exc, fallback="error.operation"):
    detail = str(exc)
    code = detail if detail.startswith("error.") and "\n" not in detail else fallback
    embedded = re.search(r"(?m)^(?:Python exception: |[\w.]+: )(error\.[a-z_]+)\s*$", detail)
    if embedded:
        code = embedded.group(1)
    lowered = detail.casefold()
    if 'error.engine_preparation_required' in lowered:
        code = 'error.engine_preparation_required'
    elif isinstance(exc, (KeyError, TypeError, NameError, AttributeError)) or re.search(
            r'(?m)^(?:KeyError|TypeError|NameError|AttributeError):', detail):
        code = 'error.internal'
    elif "frame accurate seeking is not possible" in lowered or "video track is unseekable" in lowered:
        code = "error.video_seek"
    elif "process timeout:" in lowered:
        code = "error.analysis_timeout" if fallback == "error.analysis_failed" else "error.process_timeout"
    elif any(marker in lowered for marker in ('out of memory', 'outofmemory', 'cudaerrormemoryallocation')):
        code = "error.gpu_memory"
    elif 'no space left' in lowered or 'disk full' in lowered:
        code = "error.disk_space"
    elif 'permission denied' in lowered or 'access is denied' in lowered:
        code = "error.file_access"
    elif 'driver does not support the required nvenc api version' in lowered or 'cannot load nvencodeapi' in lowered:
        code = 'error.hardware_encoder_driver'
    elif "unknown encoder 'hevc_nvenc'" in lowered or 'encoder not found: hevc_nvenc' in lowered:
        code = 'error.hardware_encoder_missing'
    elif any(marker in lowered for marker in ('does not support required nvenc', "doesn't support required nvenc",
                                               'no nvenc capable devices found', '10 bit encode not supported')):
        code = 'error.hardware_encoder_unsupported'
    elif 'fwrite() call failed' in lowered or 'broken pipe' in lowered:
        code = "error.frame_transfer"
    elif isinstance(exc, PermissionError):
        code = "error.file_access"
    elif "failed to load" in lowered or "dll load failed" in lowered:
        code = "error.setup_failed"
    source_errors = {"error.drop_path", "error.video_seek", "error.timestamp", "error.vfr", "error.hdr", "error.interlaced", "error.color", "error.no_video", "error.source_access", "error.source_changed", "error.render_format_changed"}
    setup_errors = {"error.runtime_missing", "error.setup_failed", "error.setup_required", "error.runtime_changed",
                    "error.setup_encoding", "error.denoise_model_missing", "error.unsupported_gpu", "error.model_integrity",
                    "error.hardware_encoder_driver", "error.hardware_encoder_missing", "error.hardware_encoder_unsupported"}
    recovery = ('format' if code in {'error.mp4_tracks', 'error.mp4_confirm_required'} else
                'preparation' if code == 'error.engine_preparation_required' else "source" if code in source_errors else "setup" if code in setup_errors else
                "storage" if code == "error.disk_space" else
                "output" if code in {"error.output_exists", "error.output_reserved", "error.invalid_output", "error.file_access"} else "retry")
    detail = "\n".join([detail, *getattr(exc, "__notes__", [])])
    return {"code": code, "detail": detail[-6000:], "recovery": recovery,
            "retryable": code != 'error.internal' and recovery in {"retry", "storage", "output"}}
