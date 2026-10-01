"""Single-worker queue shared by the desktop bridge and native integrations."""

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, CancelledError

from borasuki.enhance import COLOR_LIMITS, validate_grade, select_profile
from borasuki import presets
from borasuki import preparation
from borasuki.tempfiles import safe_files, orphan_job
from borasuki.batch import collect as collect_videos
from borasuki.errors import failure_info, RenderDiagnostics

from borasuki.pipeline import execute, inspect_source, source_signature, script_args
from borasuki.preflight import check as preflight
from borasuki.process import Interrupted, ProcessGroup
from borasuki.profile import validate_output_path, validate_output_tracks, processing_settings, cugan_options, cugan_model_name, cugan_tile, fast_encoding
from borasuki.runtime import discover, fingerprint, seed_cache, gpu_memory, validation_signature, validate_runtime
from borasuki.runtime_install import install_runtime
from borasuki.storage import DATA, ROOT, JobStore, atomic_json
from borasuki.preview import render as render_preview, validate_range, media_urls
from borasuki.media_server import MediaServer
from borasuki.wait_progress import WaitProgress
from borasuki.updates import UpdateCheck

logger = logging.getLogger(__name__)
ACTIVE = {"running", "pause_requested", "cancel_requested"}
HISTORY = {"completed", "failed", "cancelled"}
METRICS = ("progress", "eta", "speed", "elapsed", "vram", "frames")
CONFIG_KEYS = ("source", "output", "name", "runtime", "fingerprint", "source_signature", "media", "drop_tracks_confirmed",
               "gpu_id", "tile", "matrix", "range", "upscale", "denoise", "color_mode", "grade", "model", "encoding", "analysis", "color_overrides", "preset", "adaptive_profile", "require_engine_prepared")


class Service:
    def __init__(self, data: Path = DATA, *, start_worker=True):
        self.data = data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closing = False
        self.updates = UpdateCheck()
        self.active_id = None
        self.preview_task = None
        self.engine_task = None
        self.analysis_task = None
        self.batch_task = None
        self.inspected_sources = set()
        self.source_server = None
        self.analysis_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="color-analysis")
        self.analysis_futures = set()
        self.stop = threading.Event()
        self.active_started = None
        self.memory_sample = {}
        self.memory_sample_at = 0
        self.settings = {"language": "tr", "theme": "dark", "output_folder": "", "sound": False, "notifications": True,
                         "context_menu": False, "default_gpu": None, "queue_enabled": True,
                         "resume_retention": 30, "retention_consent": False,
                         "metrics": dict.fromkeys(METRICS, True)}
        settings_path = data / "settings.json"
        if settings_path.exists():
            try:
                self.settings.update(json.loads(settings_path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                logger.exception("Settings could not be loaded: %s", settings_path)
        self.runtime = discover()
        self.setup = self._load_setup()
        if self.runtime["ready"]:
            seed_cache(self.runtime, self.data / "engine-cache")
        self.store = JobStore(data / "jobs.sqlite3")
        self.jobs = {job["id"]: job for job in self.store.load()}
        now = time.time()
        for index, job in enumerate(self.jobs.values()):
            # Older releases always used 2x / noise=1; their mode meant color.
            for config in [job, *([job["configuration"]] if "configuration" in job else [])]:
                if "color_mode" not in config:
                    config["color_mode"] = config.pop("mode", "reference")
                config.setdefault("upscale", {"scale": 2})
                config.setdefault("denoise", {"enabled": True, "strength": 1})
            job.setdefault("order", index)
            job.setdefault("elapsed", 0)
            job.setdefault("revision", 0)
            job.setdefault("resume_deleted", False)
            if job["status"] == "cancel_requested":
                job.update(status="cancelled", finished=now)
            elif job["status"] == "pause_requested":
                job.update(status="paused", paused_at=now)
            elif job["status"] in ("running", "editing"):
                job.update(status="queued", stage="recovering", recover_published=True, speed=None, eta=None)
            if job["status"] == "paused":
                job.setdefault("paused_at", now)
            job.pop("edit_token", None)
            if job.get("error") and not job.get("failure"):
                job["failure"] = self._legacy_failure(job)
            self.store.save(job)
        self.worker = threading.Thread(target=self._loop, name="render-queue", daemon=True)
        if start_worker:
            self.worker.start()
            self.wake.set()

    def _legacy_failure(self, job):
        failure = failure_info(RuntimeError(job["error"]))
        if failure["code"] != "error.operation":
            return failure
        try:
            logfile = self._owned_work(job) / 'render.log'
            with logfile.open('rb') as stream:
                stream.seek(max(0, logfile.stat().st_size - 1024 * 1024))
                diagnostic = RenderDiagnostics()
                for line in stream.read().decode('utf-8', 'replace').splitlines():
                    if line.startswith('Input #0, yuv4mpegpipe'):
                        diagnostic = RenderDiagnostics()
                    diagnostic.add(line)
            return failure_info(RuntimeError(diagnostic.detail() or job["error"]))
        except FileNotFoundError:
            return failure
        except (OSError, ValueError):
            logger.warning('Could not recover render evidence for job %s', job['id'], exc_info=True)
            return failure

    def _load_setup(self):
        if self.runtime["ready"]:
            path = self.data / "runtime-validation.json"
            if path.exists():
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if saved.get("signature") == validation_signature(self.runtime):
                        return {"status": "ready", **saved}
                except (ValueError, OSError):
                    logger.exception("Runtime validation receipt could not be loaded")
        return {"status": "waiting" if self.runtime["ready"] else "missing", "stage": "files", "error": None}

    def _require_setup(self, runtime):
        with self.lock:
            if self.closing:
                raise ValueError('error.app_closing')
            if not runtime["ready"]:
                raise ValueError("error.runtime_missing")
            if self.setup["status"] == "ready":
                try:
                    if self.setup.get("signature") == validation_signature(runtime):
                        return
                except OSError:
                    logger.info("Runtime files changed since verification", exc_info=True)
            if self.setup["status"] == "ready":
                self.setup = {"status": "waiting", "stage": "files", "error": None}
                self.wake.set()
            raise ValueError("error.setup_required")

    def request_setup(self):
        with self.lock:
            if (self.active_id is not None or self.preview_task and self.preview_task["status"] in ("waiting", "preparing", "rendering")
                    or self.engine_task and self.engine_task['status'] in ('waiting', 'preparing')
                    or self.batch_task and self.batch_task['status'] in ('waiting', 'preparing', 'stopping')):
                raise ValueError("error.setup_busy")
            if self.setup["status"] in ("waiting", "checking"):
                return
            (self.data / "runtime-validation.json").unlink(missing_ok=True)
            self.setup = {"status": "waiting", "stage": "files", "error": None}
        self.wake.set()

    def _run_setup(self):
        def publish(**fields):
            with self.lock:
                self.setup.update(fields)
        progress = WaitProgress(publish)
        progress.set('setup_checks', 0, 3)
        def update(**fields):
            with self.lock:
                self.setup.update(fields)
            if fields.get('stage') in ('models', 'plugins', 'encoding'):
                progress.set('setup_checks', {'models': 0, 'plugins': 1, 'encoding': 2}[fields['stage']], 3)
        try:
            (self.data / "runtime-validation.json").unlink(missing_ok=True)
            runtime = discover()
            with self.lock:
                self.runtime = runtime
            if not runtime["ready"]:
                update(status="checking", stage="download", error=None, failure=None)
                def installation(**fields):
                    stage = fields.get('stage')
                    if stage == 'download':
                        progress.set('setup_download', fields['downloaded'], fields['total'])
                        with self.lock:
                            self.setup['package'] = fields['package']
                            self.setup['progress']['unit'] = 'bytes'
                    elif stage in ('extract', 'installed'):
                        progress.set('setup_' + stage)
                    update(**fields)
                runtime = install_runtime(self.data, self.stop, installation, runtime['gpus'])
                with self.lock:
                    self.runtime = runtime
            result = validate_runtime(runtime, self.data, self.stop, update)
            if self.stop.is_set():
                raise Interrupted()
            atomic_json(self.data / "runtime-validation.json", result)
            update(status="ready", stage="complete", error=None, failure=None, **result)
        except Interrupted:
            update(status="waiting")
        except Exception as exc:
            logger.exception("Runtime compatibility check failed")
            failure = failure_info(exc, "error.setup_failed")
            update(status="failed", error=failure["code"], failure=failure)

    def request_preparation(self, source, upscale, denoise, gpu_id):
        runtime = discover()
        self._require_setup(runtime)
        upscale, denoise = processing_settings(upscale, denoise)
        if type(gpu_id) is not int or gpu_id not in [g['id'] for g in runtime['gpus']]:
            raise ValueError('error.unsupported_gpu')
        media = self.inspect(source, runtime)
        with self.lock:
            if self.closing:
                raise ValueError('error.app_closing')
            task = self.engine_task
            config = {'runtime': runtime, 'media': {key: media[key] for key in ('width', 'height', 'fps_num', 'fps_den') if key in media}, 'gpu_id': gpu_id,
                      'upscale': upscale, 'denoise': denoise, 'cache': str(self.data / 'engine-cache'),
                      'encoding': fast_encoding(), 'matrix': media['matrix']}
            config['tile'] = self._selected_tile(config, self._tile_free_vram(runtime, gpu_id))
            key = preparation.identity(config)[0]
            if task and task['key'] == key and task['status'] in ('waiting', 'preparing') and not task['stop'].is_set():
                return task['id']
            if task:
                task['stop'].set()
            self.engine_task = {'id': uuid.uuid4().hex, 'key': key, 'config': config,
                                'status': 'ready' if preparation.ready(config) else 'waiting',
                                'stop': threading.Event(), 'error': None, 'failure': None}
            token = self.engine_task['id']
        self.wake.set()
        return token

    def _tile_free_vram(self, runtime, gpu_id):
        with self.lock:
            gpu_busy = bool(self.active_id or self.engine_task and self.engine_task['status'] == 'preparing'
                            or self.preview_task and self.preview_task['status'] in ('preparing', 'rendering'))
            source = self.runtime if gpu_busy else runtime
            return next((gpu['free'] for gpu in source['gpus'] if gpu['id'] == gpu_id), 0)

    def _selected_tile(self, config, free):
        task = self.engine_task
        if task and task['status'] in ('waiting', 'preparing', 'ready'):
            old = task['config']
            if (all(old[name] == config[name] for name in ('gpu_id', 'upscale', 'denoise'))
                    and all(old['media'][name] == config['media'][name] for name in ('width', 'height'))):
                return list(old['tile'])
        return cugan_tile(config['media']['width'], config['media']['height'], free)

    def cancel_preparation(self, token):
        with self.lock:
            task = self.engine_task
            if task and task['id'] == token:
                task['stop'].set()
                if task['status'] == 'waiting':
                    task['status'] = 'cancelled'
        self.wake.set()

    def _run_preparation(self, task):
        def update(**fields):
            with self.lock:
                task.update(fields)
        try:
            preparation.prepare(task['config'], self.data, task['stop'], update=update)
            with self.lock:
                task['status'] = 'cancelled' if task['stop'].is_set() else 'ready'
        except Interrupted:
            with self.lock:
                task['status'] = 'cancelled'
        except Exception as exc:
            failure = failure_info(exc, 'error.engine_preparation_failed')
            logger.exception('Selected-settings preparation failed key=%s', task['key'])
            with self.lock:
                task.update(status='failed', error=failure['code'], failure=failure)

    def inspect(self, source: str, runtime=None, stop=None) -> dict:
        runtime = runtime if runtime is not None else self.runtime
        if not runtime["ready"]:
            raise ValueError("error.runtime_missing")
        path = Path(source).resolve(strict=True)
        if not path.is_file():
            raise ValueError("error.no_video")
        with self.lock:
            self.inspected_sources.add(str(path))
        return {"path": str(path), "name": path.name, **inspect_source(path, runtime, stop or threading.Event())}

    def source_media(self, source):
        path = Path(source).resolve(strict=True)
        with self.lock:
            if self.closing:
                raise ValueError('error.app_closing')
            if str(path) not in self.inspected_sources or not path.is_file():
                raise ValueError('error.source_access')
            if self.source_server is None:
                self.source_server = MediaServer()
            return {'url':self.source_server.grant(path)}

    @staticmethod
    def _analyze_config(config, stop, update=None):
        path = Path(config["work"]) / "config.json"
        atomic_json(path, {**config, "root": str(ROOT)})
        progress = WaitProgress(update or (lambda **fields: None))
        progress.set('source_reading')
        with ProcessGroup(stop) as group:
            group.capture(script_args(path, config["runtime"], "analyze"), timeout=600,
                          on_poll=lambda: progress.poll(path.parent / 'wait-progress.json'))
        progress.poll(path.parent / 'wait-progress.json', force=True)
        analysis = json.loads((path.parent / "analysis.json").read_text(encoding="utf-8"))
        validate_grade({key: analysis[key] for key in COLOR_LIMITS})
        return analysis

    def request_analysis(self, source, editing=None):
        source_id = source_signature(Path(source))
        with self.lock:
            if self.closing:
                raise ValueError("error.invalid_action")
            saved = None
            if editing:
                old = self.jobs.get(editing.get("id"))
                if not old or old["status"] != "editing" or old.get("edit_token") != editing.get("token") or self.active_id == old["id"]:
                    raise ValueError("error.job_locked")
                if old["source_signature"] != source_id:
                    raise ValueError("error.source_changed")
                if old["color_mode"] == "adaptive" and old.get("analysis"):
                    saved = copy.deepcopy(old)
            self._cancel_analysis()
            task = {"id": uuid.uuid4().hex, "status": "waiting", "source_signature": source_id,
                    "source": str(Path(source).resolve()), "error": None, "stop": threading.Event()}
            self.analysis_task = task
            if saved:
                task.update(status="ready", analysis=saved["analysis"], overrides=saved.get("color_overrides", {}))
            else:
                task["future"] = self.analysis_pool.submit(self._run_analysis, task)
                self.analysis_futures.add(task['future'])
                task['future'].add_done_callback(self._analysis_finished)
            return task["id"]

    def _analysis_finished(self, future):
        with self.lock:
            self.analysis_futures.discard(future)

    def _cancel_analysis(self):
        task = self.analysis_task
        if task:
            task["stop"].set()
            if task.get("future"):
                task["future"].cancel()
            task["status"] = "cancelled"

    def cancel_analysis(self, token):
        with self.lock:
            # Late cancellation must never cancel a newer source's analysis.
            if self.analysis_task and self.analysis_task["id"] == token:
                self._cancel_analysis()

    def _run_analysis(self, task):
        started = time.monotonic()
        def update(**fields):
            if 'progress' in fields:
                fields['progress']['elapsed'] = time.monotonic() - started
            with self.lock:
                task.update(fields)
        progress = WaitProgress(update)
        progress.set('source_reading')
        try:
            with self.lock:
                if task["stop"].is_set():
                    raise Interrupted()
                task["status"] = "analyzing"
            runtime = discover()
            media = self.inspect(task["source"], runtime, task["stop"])
            # CPU sampling only; no model inference or GPU ownership.
            with tempfile.TemporaryDirectory(prefix="color-", dir=self.data) as folder:
                analysis = self._analyze_config({"source": task["source"], "media": media, "runtime": runtime,
                    "work": folder, "matrix": media["matrix"], "range": media["range"]}, task["stop"], update=update)
            if source_signature(Path(task["source"])) != task["source_signature"]:
                raise ValueError("error.source_changed")
            with self.lock:
                if task["stop"].is_set():
                    raise Interrupted()
                task.update(status="ready", analysis=analysis, overrides={})
            logger.info("Color analysis completed id=%s source=%s seek_mode=%s", task["id"], task["source"], analysis.get("seek_mode"))
        except Interrupted:
            with self.lock:
                task["status"] = "cancelled"
        except Exception as exc:
            logger.exception("Color analysis failed id=%s source=%s", task["id"], task["source"])
            failure = failure_info(exc, "error.analysis_failed")
            with self.lock:
                task.update(status="failed", error=failure["code"], failure=failure)

    def _resolve_adaptive(self, source, adaptive):
        if not isinstance(adaptive, dict):
            raise ValueError("error.analysis_stale")
        with self.lock:
            task = self.analysis_task
            if not task or task["id"] != adaptive.get("token") or task["status"] != "ready":
                raise ValueError("error.analysis_stale")
            if source_signature(Path(source)) != task["source_signature"]:
                raise ValueError("error.source_changed")
            overrides = validate_grade(adaptive.get("overrides", {}), partial=True)
            analysis = select_profile(task['analysis'], adaptive.get('profile', 'normal'))
            return {"analysis": copy.deepcopy(analysis), "overrides": overrides,
                    "source_signature": copy.deepcopy(task["source_signature"])}

    def _prepare(self, source: str, output: str, color_mode: str, gpu_id: int, custom: dict,
                 identifier: str, revision=0, upscale=None, denoise=None, stop=None, adaptive=None, color_snapshot=None, work=None, preset=None, color_recipe=None, drop_tracks_confirmed=False) -> dict:
        if color_mode == "adaptive" and adaptive is not None:
            color_snapshot = self._resolve_adaptive(source, adaptive)
        upscale, denoise = processing_settings(upscale, denoise)
        runtime = discover()
        self._require_setup(runtime)
        try:
            source_id = source_signature(Path(source))
        except OSError as exc:
            raise ValueError("error.source_access") from exc
        media = self.inspect(source, runtime, stop)
        path = Path(media["path"])
        target = Path(output)
        try:
            validate_output_path(target)
        except ValueError as exc:
            raise ValueError("error.invalid_output") from exc
        validate_output_tracks(target, media, drop_tracks_confirmed)
        target = target.resolve()
        if str(path).casefold() == str(target).casefold() or target.exists():
            raise ValueError("error.output_exists")
        if color_mode not in ("original", "reference", "adaptive", "custom"):
            raise ValueError("error.invalid_settings")
        if type(gpu_id) is not int or gpu_id not in [gpu["id"] for gpu in runtime["gpus"]]:
            raise ValueError("error.unsupported_gpu")
        grade = {"contrast": 1.03 if color_mode == "reference" else 1.0, "brightness": 0.0, "saturation": 1.0}
        if color_mode == "custom":
            grade = validate_grade(custom)
        with self.lock:
            tile = self._selected_tile({'gpu_id': gpu_id, 'media': media, 'upscale': upscale, 'denoise': denoise},
                                       self._tile_free_vram(runtime, gpu_id))
        noise = cugan_options({"upscale": upscale, "denoise": denoise, "tile": tile})["noise"]
        model_name = cugan_model_name(noise)
        model = Path(runtime["plugins"]) / "models/cugan" / model_name
        if not model.is_file():
            raise ValueError("error.denoise_model_missing")
        job = {"id": identifier, "source": str(path), "output": str(target), "name": path.name,
               "work": str(work or self.data / "jobs" / identifier / f"revision-{revision}"), "cache": str(self.data / "engine-cache"),
               "runtime": copy.deepcopy(runtime), "fingerprint": fingerprint(runtime, gpu_id, noise),
               "source_signature": source_id, "media": media, "gpu_id": gpu_id,
               "drop_tracks_confirmed": drop_tracks_confirmed,
               "tile": tile, "matrix": media["matrix"], "range": media["range"], "color_mode": color_mode, "grade": grade,
               "upscale": upscale, "denoise": denoise,
               "status": "queued", "stage": "queued", "created": time.time(), "frames": 0,
               "total_frames": None, "eta": None, "speed": None, "error": None,
               "revision": revision, "elapsed": 0, "resume_deleted": False}
        job["model"] = {"name": "Real-CUGAN", "version": hashlib.sha256(model.read_bytes()).hexdigest(),
                        "file": model_name, "scale": upscale["scale"], "noise": noise, "fp16": True}
        job["encoding"] = fast_encoding()
        # Only prepared settings may enter Preview or Queue.
        job['require_engine_prepared'] = True
        job["preflight"] = preflight(job, runtime)
        if color_mode == "adaptive":
            if color_snapshot and color_snapshot["source_signature"] != source_id:
                raise ValueError("error.source_changed")
            job["analysis"] = color_snapshot["analysis"] if color_snapshot else self._analyze_config(job, stop or threading.Event())
            if color_recipe:
                job['analysis'] = select_profile(job['analysis'], color_recipe['adaptive_profile'])
            job['adaptive_profile'] = job['analysis'].get('profile', 'normal')
            job["color_overrides"] = color_snapshot["overrides"] if color_snapshot else color_recipe.get('color_overrides', {}) if color_recipe else {}
            job["grade"] = validate_grade({**{key: job["analysis"][key] for key in COLOR_LIMITS}, **job["color_overrides"]})
        if source_signature(path) != job["source_signature"]:
            raise ValueError("error.source_changed")
        if preset is not None:
            saved = next((item for item in self.list_presets() if item['id'] == preset), None)
            if not saved or presets.from_job(job) != presets.settings(saved['values']):
                raise ValueError('error.preset_changed')
            job['preset'] = saved
        job["configuration"] = {key: copy.deepcopy(job[key]) for key in CONFIG_KEYS if key in job}
        preparation.require(job)
        return job

    def request_batch(self, paths, output_folder, values, gpu_id, preset=None, output_format='mkv', drop_tracks_confirmed=False):
        selection = collect_videos(paths, folders=False)
        recipe = presets.settings(values)
        if output_format not in ('mkv', 'mp4') or type(drop_tracks_confirmed) is not bool:
            raise ValueError('error.invalid_settings')
        if output_format == 'mp4' and not drop_tracks_confirmed:
            raise ValueError('error.mp4_confirm_required')
        if not isinstance(output_folder, str) or not output_folder.strip():
            raise ValueError('error.invalid_output')
        folder = Path(output_folder).resolve()
        if not folder.is_dir():
            raise ValueError('error.invalid_output')
        with self.lock:
            self._require_setup(self.runtime)
            if self.batch_task and self.batch_task['status'] in ('waiting', 'preparing', 'stopping'):
                raise ValueError('error.batch_busy')
            if type(gpu_id) is not int or gpu_id not in [gpu['id'] for gpu in self.runtime['gpus']]:
                raise ValueError('error.unsupported_gpu')
            saved = next((item for item in self.list_presets() if item['id'] == preset), None) if preset else None
            if preset and (not saved or presets.settings(saved['values']) != recipe):
                raise ValueError('error.preset_changed')
            task = {'id': uuid.uuid4().hex, 'status': 'waiting', 'stop': threading.Event(),
                    'items': [{'path': path, 'status': 'pending'} for path in selection['paths']] +
                             [{**item, 'status': 'failed'} for item in selection['failures']],
                    'recipe': copy.deepcopy(recipe), 'output_folder': str(folder), 'output_format': output_format,
                    'drop_tracks_confirmed': drop_tracks_confirmed, 'gpu_id': gpu_id,
                    'preset': copy.deepcopy(saved)}
            self.batch_task = task
            task['future'] = self.analysis_pool.submit(self._run_batch, task)
            self.analysis_futures.add(task['future'])
            task['future'].add_done_callback(self._analysis_finished)
            return task['id']

    def cancel_batch(self, token):
        with self.lock:
            task = self.batch_task
            if not task or task['id'] != token:
                raise ValueError('error.batch_stale')
            if task['status'] not in ('waiting', 'preparing', 'stopping'):
                return
            task['stop'].set()
            task['status'] = 'stopping'
            if task['future'].cancel():
                task['status'] = 'cancelled'
                for item in task['items']:
                    if item['status'] == 'pending':
                        item['status'] = 'not_added'

    def _run_batch(self, task):
        recipe = task['recipe']
        with self.lock:
            task['progress'] = {'elapsed': 0, 'updated_at': time.time(), 'eta': None}
        try:
            with self.lock:
                task['status'] = 'preparing'
            for item in task['items']:
                if item['status'] != 'pending':
                    continue
                if task['stop'].is_set():
                    break
                with self.lock:
                    item['status'] = 'preparing'
                try:
                    output = Path(task['output_folder']) / (Path(item['path']).stem + '_borasuki_2x.' + task['output_format'])
                    job = self._prepare(item['path'], str(output), recipe['color_mode'], task['gpu_id'], recipe.get('custom', {}),
                        uuid.uuid4().hex, upscale=recipe['upscale'], denoise=recipe['denoise'], stop=task['stop'], color_recipe=recipe,
                        drop_tracks_confirmed=task['drop_tracks_confirmed'])
                    if task['preset']:
                        job['preset'] = copy.deepcopy(task['preset'])
                        job['configuration']['preset'] = copy.deepcopy(task['preset'])
                    with self.lock:
                        if task['stop'].is_set() or self.closing:
                            raise Interrupted()
                        preparation.require(job)
                        self._reserve_output(job)
                        job['order'] = max((j['order'] for j in self.jobs.values()), default=-1) + 1
                        self._commit(job)
                        item.update(status='added', job_id=job['id'], output=job['output'])
                    self.wake.set()
                except Interrupted:
                    break
                except Exception as exc:
                    logger.exception('Batch import failed batch=%s source=%s', task['id'], item['path'])
                    with self.lock:
                        item.update(status='failed', failure=failure_info(exc))
        finally:
            with self.lock:
                for item in task['items']:
                    if item['status'] in ('pending', 'preparing'):
                        item['status'] = 'not_added'
                task['status'] = 'cancelled' if task['stop'].is_set() else 'complete'

    def list_presets(self):
        with self.lock:
            return self.store.presets()

    def save_preset(self, name, values):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(char) < 32 for char in name):
            raise ValueError('error.preset_name')
        preset = {'id': uuid.uuid4().hex, 'name': name.strip(), 'values': presets.settings(values), 'created': time.time()}
        with self.lock:
            if any(item['name'].casefold() == preset['name'].casefold() for item in self.store.presets()):
                raise ValueError('error.preset_duplicate')
            self.store.save_preset(preset)
        return preset

    def delete_preset(self, identifier, confirmed=False):
        if confirmed is not True:
            raise ValueError('error.confirmation')
        with self.lock:
            self.store.delete_preset(identifier)

    def _reserve_output(self, job):
        if any(j["id"] != job["id"] and j["output"].casefold() == job["output"].casefold()
               and j["status"] not in HISTORY for j in self.jobs.values()):
            raise ValueError("error.output_reserved")

    def check_draft(self, values, editing=None, saved=False):
        if not isinstance(values, dict) or type(saved) is not bool or saved and not editing:
            raise ValueError('error.invalid_settings')
        with self.lock:
            current = self.jobs.get(editing.get('id')) if editing else None
            if editing and (not current or current['status'] != 'editing' or current.get('edit_token') != editing.get('token')):
                raise ValueError('error.job_locked')
            stored = copy.deepcopy({**current, **current.get('configuration', {})}) if saved else None
        if stored:
            runtime = discover()
            self._require_setup(runtime)
            checks = preflight(stored, runtime)
            preparation.require(stored)
            job = stored
        else:
            if values.get('color_mode') == 'adaptive' and values.get('adaptive') is None:
                raise ValueError('error.analysis_stale')
            with tempfile.TemporaryDirectory(prefix='preflight-', dir=self.data) as work:
                job = self._prepare(values['source'], values['output'], values['color_mode'], values['gpu_id'],
                    values['custom'], uuid.uuid4().hex, upscale=values['upscale'], denoise=values['denoise'],
                    adaptive=values.get('adaptive'), work=work, preset=values.get('preset'),
                    drop_tracks_confirmed=values.get('drop_tracks_confirmed', False))
                checks = job['preflight']
        with self.lock:
            self._reserve_output({**job, 'id': editing['id']} if editing else job)
        return checks

    def request_preview(self, values, start=0, duration=3, editing=None, saved=False):
        validate_range(start, duration)
        if not isinstance(values, dict) or type(saved) is not bool or saved and not editing:
            raise ValueError('error.invalid_settings')
        if not saved and values.get('color_mode') == 'adaptive' and values.get('adaptive') is None:
            raise ValueError('error.analysis_stale')
        with self.lock:
            self._require_setup(self.runtime)
        self.check_draft(values, editing, saved)
        with self.lock:
            self._require_setup(self.runtime)
            if self.preview_task and self.preview_task['status'] in ('waiting', 'preparing', 'rendering'):
                raise ValueError('error.preview_busy')
            if editing:
                current = self.jobs.get(editing.get('id'))
                if not current or current['status'] != 'editing' or current.get('edit_token') != editing.get('token'):
                    raise ValueError('error.job_locked')
            task = {'id': uuid.uuid4().hex, 'status': 'waiting', 'values': copy.deepcopy(values),
                    'editing': copy.deepcopy(editing), 'saved': saved, 'start': start, 'duration': duration,
                    'frames': 0, 'total_frames': None, 'error': None, 'stop': threading.Event()}
            if not saved and values.get('color_mode') == 'adaptive' and values.get('adaptive') is not None:
                task['color_snapshot'] = self._resolve_adaptive(values['source'], values['adaptive'])
            self.preview_task = task
        self.wake.set()
        return task['id']

    def cancel_preview(self, token):
        with self.lock:
            task = self.preview_task
            if not task or task['id'] != token:
                raise ValueError('error.preview_stale')
            task['stop'].set()
            if task['status'] in ('waiting', 'ready'):
                task['status'] = 'cancelled'
        self.wake.set()

    def preview_media(self, token):
        with self.lock:
            task = self.preview_task
            if not task or task['id'] != token or task['status'] != 'ready':
                raise ValueError('error.preview_stale')
            files = dict(task['files'])
        return media_urls(files)

    def commit_preview(self, token):
        with self.lock:
            task = self.preview_task
            if not task or task['id'] != token or task['status'] != 'ready':
                raise ValueError('error.preview_stale')
            job = copy.deepcopy(task['job'])
            editing = task['editing']
            if editing:
                old = self.jobs.get(editing['id'])
                if not old or old['status'] != 'editing' or old.get('edit_token') != editing['token'] or self.active_id == old['id']:
                    raise ValueError('error.job_locked')
                job.update(id=old['id'], order=old['order'], created=old['created'], revision=old['revision'] + 1)
                # Keep edit cleanup ownership compatible with job revisions.
                destination = self.data / 'jobs' / old['id'] / f"revision-{job['revision']}"
                while destination.exists():
                    job['revision'] += 1
                    destination = self.data / 'jobs' / old['id'] / f"revision-{job['revision']}"
                job['work'] = str(destination)
            else:
                job['order'] = max((j['order'] for j in self.jobs.values()), default=-1) + 1
            runtime = discover()
            self._require_setup(runtime)
            job['preflight'] = preflight(job, runtime)
            if job.get('require_engine_prepared'):
                preparation.require(job)
            self._reserve_output(job)
            self._commit(job)
            task['status'] = 'committed'
        self.wake.set()
        return job['id']

    def _run_preview(self, task):
        started = time.monotonic()
        def update(**fields):
            if 'progress' in fields:
                fields['progress']['elapsed'] = time.monotonic() - started
            with self.lock:
                task.update(fields)
        WaitProgress(update).set('source_reading')
        try:
            values = task['values']
            if task['saved']:
                with self.lock:
                    old = self.jobs.get(task['editing']['id'])
                    if not old or old['status'] != 'editing' or old.get('edit_token') != task['editing']['token']:
                        raise ValueError('error.job_locked')
                    job = copy.deepcopy({**old, **old.get('configuration', {})})
                job.update(id=task['id'], work=str(self.data / 'jobs' / task['id'] / 'revision-0'), revision=0,
                           status='queued', stage='queued', frames=0, total_frames=None, elapsed=0, error=None)
                job.pop('edit_token', None)
            else:
                job = self._prepare(values['source'], values['output'], values['color_mode'], values['gpu_id'],
                                    values['custom'], task['id'], upscale=values['upscale'], denoise=values['denoise'],
                                    stop=task['stop'], color_snapshot=task.get('color_snapshot'), preset=values.get('preset'),
                                    drop_tracks_confirmed=values.get('drop_tracks_confirmed', False))
            if task['stop'].is_set():
                raise Interrupted()
            runtime = discover()
            self._require_setup(runtime)
            job['runtime'] = runtime
            if not preparation.ready(job):
                update(status='preparing', stage='engine_preparing')
                preparation.prepare(job, self.data, task['stop'], update=update)
            preparation.require(job)
            job['require_engine_prepared'] = True
            job['configuration'] = {key: copy.deepcopy(job[key]) for key in CONFIG_KEYS if key in job}
            job['preflight'] = preflight(job, runtime)
            with self.lock:
                self._reserve_output({**job, 'id': task['editing']['id']} if task['editing'] else job)
            result = render_preview(job, task['start'], task['duration'], task['stop'], update)
            with self.lock:
                if task['stop'].is_set():
                    raise Interrupted()
                task.update(result, job=job, status='ready')
        except Interrupted:
            update(status='cancelled')
        except Exception as exc:
            logger.exception('Preview failed id=%s', task['id'])
            failure = failure_info(exc)
            update(status='failed', error=failure['code'], failure=failure)

    def _commit(self, job, *, runtime_update=False):
        if self.closing and not runtime_update:
            raise ValueError('error.app_closing')
        self.store.save(job)
        self.jobs[job["id"]] = job

    def create(self, source: str, output: str, color_mode: str, gpu_id: int, custom: dict, upscale=None, denoise=None, adaptive=None, preset=None, drop_tracks_confirmed=False) -> str:
        if color_mode == 'adaptive' and adaptive is None:
            raise ValueError('error.analysis_stale')
        identifier = uuid.uuid4().hex
        job = self._prepare(source, output, color_mode, gpu_id, custom, identifier, upscale=upscale, denoise=denoise,
                            adaptive=adaptive, preset=preset, drop_tracks_confirmed=drop_tracks_confirmed)
        with self.lock:
            preparation.require(job)
            self._reserve_output(job)
            job["order"] = max((j["order"] for j in self.jobs.values()), default=-1) + 1
            self._commit(job)
        self.wake.set()
        return identifier

    def action(self, identifier: str, action: str, confirmed=False):
        with self.lock:
            job = copy.deepcopy(self.jobs[identifier])
            status = job["status"]
            if action == "cancel" and confirmed is not True:
                raise ValueError("error.confirmation")
            if (action == "pause" and status == "running" and self.active_id == identifier
                    and job.get("stage") != "engine_preparing"):
                job["status"] = "pause_requested"
            elif action == "cancel" and status in {"running", "pause_requested", "paused"}:
                if self.active_id == identifier:
                    job["status"] = "cancel_requested"
                else:
                    job.update(status="cancelled", finished=time.time())
            elif (action == "resume" and status == "paused" or action == "retry" and status == "failed") and self.active_id != identifier:
                if action == "resume" and job.get("resume_deleted"):
                    raise ValueError("error.resume_deleted")
                if Path(job["output"]).exists():
                    raise ValueError("error.output_exists")
                if job.get('require_engine_prepared'):
                    preparation.require(job)
                self._reserve_output(job)
                job.update(status="queued", error=None, failure=None, stage="queued", resume_deleted=False)
                job.pop("finished", None)
            else:
                raise ValueError("error.invalid_action")
            self._commit(job)
            if self.active_id == identifier and action in ("pause", "cancel"):
                self.stop.set()
        self.wake.set()

    def begin_edit(self, identifier):
        with self.lock:
            job = copy.deepcopy(self.jobs[identifier])
            if self.active_id == identifier or job["status"] != "queued":
                raise ValueError("error.job_locked")
            job.update(status="editing", edit_token=uuid.uuid4().hex)
            self._commit(job)
            return copy.deepcopy(job)

    def end_edit(self, identifier, token, values=None):
        with self.lock:
            old = copy.deepcopy(self.jobs[identifier])
            if old["status"] != "editing" or old.get("edit_token") != token or self.active_id == identifier:
                raise ValueError("error.job_locked")
        job = self._prepare(values["source"], values["output"], values["color_mode"], values["gpu_id"], values["custom"],
                            identifier, old["revision"] + 1, values["upscale"], values["denoise"], adaptive=values.get("adaptive"),
                            preset=values.get('preset'), drop_tracks_confirmed=values.get('drop_tracks_confirmed', False)) if values is not None else old
        with self.lock:
            current = self.jobs[identifier]
            if current["status"] != "editing" or current.get("edit_token") != token:
                raise ValueError("error.job_locked")
            if values is not None:
                preparation.require(job)
            self._reserve_output(job)
            job.update(order=old["order"], created=old["created"], status="queued")
            job.pop("edit_token", None)
            self._commit(job)
        self.wake.set()

    def reorder(self, identifiers):
        with self.lock:
            waiting = sorted((j for j in self.jobs.values() if j["status"] == "queued" and j["id"] != self.active_id), key=lambda j: j["order"])
            if not isinstance(identifiers, list) or len(identifiers) != len(set(identifiers)) or set(identifiers) != {j["id"] for j in waiting}:
                raise ValueError("error.queue_changed")
            changed = [{**self.jobs[identifier], "order": slot["order"]} for identifier, slot in zip(identifiers, waiting)]
            self.store.save_many(changed)
            self.jobs.update({j["id"]: j for j in changed})

    def move(self, identifier, direction):
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError("error.invalid_action")
        with self.lock:
            waiting = [j["id"] for j in sorted(self.jobs.values(), key=lambda j: j["order"])
                       if j["status"] == "queued" and j["id"] != self.active_id]
            if identifier not in waiting:
                raise ValueError("error.job_locked")
            index = waiting.index(identifier)
            target = index + direction
            if not 0 <= target < len(waiting):
                raise ValueError("error.queue_changed")
            waiting[index], waiting[target] = waiting[target], waiting[index]
            self.reorder(waiting)

    @staticmethod
    def available_actions(job, locked):
        status = job["status"]
        actions = ["details"]
        if status == "running":
            actions += ["cancel"] if job.get("stage") == "engine_preparing" else ["pause", "cancel"]
        elif status == "pause_requested":
            actions += ["cancel"]
        if locked:
            return actions
        if status == "queued":
            actions += ["edit", "preview", "remove", "reorder"]
        elif status == "paused":
            actions += ["cancel"]
            if not job.get("resume_deleted"):
                actions += ["resume", "cleanup_resume"]
        elif status in HISTORY:
            actions += ["remove"]
            if status == "failed":
                actions += ["retry"]
            elif status == "completed":
                actions += ["open_output"]
        return actions

    def remove(self, scope, identifier=None, confirmed=False):
        if confirmed is not True:
            raise ValueError("error.confirmation")
        with self.lock:
            allowed = HISTORY if scope == "history" else {"queued"} if scope == "queue" else set()
            if not allowed:
                raise ValueError("error.invalid_action")
            selected = [self.jobs[identifier]] if identifier else list(self.jobs.values())
            if identifier and (selected[0]["status"] not in allowed or identifier == self.active_id):
                raise ValueError("error.job_locked")
            ids = [j["id"] for j in selected if j["status"] in allowed and j["id"] != self.active_id]
            self.store.delete_many(ids)
            for key in ids:
                del self.jobs[key]
            return len(ids)

    def _owned_work(self, job):
        root = self.data / "jobs" / job["id"]
        if len(job["id"]) != 32 or any(c not in "0123456789abcdef" for c in job["id"]):
            raise ValueError("error.unsafe_cleanup")
        work = Path(job["work"])
        if work not in (root, root / f"revision-{job['revision']}"):
            raise ValueError("error.unsafe_cleanup")
        safe_files(work, [*self._protected_files(), job['source'], job['output']])
        return work

    def _protected_files(self):
        with self.lock:
            paths = list(self.inspected_sources)
            for job in self.jobs.values():
                paths.extend(job[key] for key in ('source', 'output'))
            if self.preview_task:
                paths.extend(self.preview_task.get('values', {}).get(key) for key in ('source', 'output'))
            if self.analysis_task:
                paths.append(self.analysis_task.get('source'))
            if self.batch_task:
                paths.extend(item['path'] for item in self.batch_task['items'])
            return [path for path in paths if path]

    def cleanup_resume(self, identifier, confirmed=False):
        self._cleanup_job_files(identifier, confirmed, all_revisions=False)

    def storage_usage(self):
        with self.lock:
            jobs = copy.deepcopy(list(self.jobs.values()))
            active = self.active_id
        rows = []
        for job in jobs:
            row = {'id':job['id'], 'name':job['name'], 'status':job['status'], 'bytes':0,
                   'cleanable':job['id'] != active and job['status'] in HISTORY | {'paused'}}
            try:
                work = self._owned_work({**job, 'work':str(self.data / 'jobs' / job['id'])})
                for parent, _, files in os.walk(work, followlinks=False):
                    for name in files:
                        try:
                            row['bytes'] += (Path(parent) / name).stat(follow_symlinks=False).st_size
                        except FileNotFoundError:
                            continue
            except (OSError, ValueError):
                row.update(bytes=None, cleanable=False, error='error.storage_scan')
            rows.append(row)
        with self.lock:
            extras = self._storage_extras()
        return {'jobs':rows, 'extras':[{**{key:value for key,value in row.items() if key not in ('path', 'files', 'owner', 'partials')},
                                      'location':str(row.get('path', ''))} for row in extras],
                'bytes':sum(row['bytes'] or 0 for row in [*rows, *extras]),
                'incomplete':any(row['bytes'] is None for row in [*rows, *extras])}

    def _temp_busy(self):
        return (self.closing or self.active_id is not None or any(job['status'] in ACTIVE | {'editing'} for job in self.jobs.values())
                or self.engine_task and self.engine_task['status'] in ('waiting', 'preparing')
                or any(not future.done() for future in self.analysis_futures)
                or self.preview_task and self.preview_task['status'] in ('waiting', 'preparing', 'rendering')
                or self.setup['status'] in ('waiting', 'checking'))

    @staticmethod
    def _partial_path(job):
        identifier = job['id']
        if not re.fullmatch('[0-9a-f]{32}', identifier):
            raise ValueError('error.unsafe_cleanup')
        output = Path(job['output'])
        return output.parent / f'.borasuki-{identifier}.partial{output.suffix.lower()}'

    def _storage_extras(self):
        protected, rows = self._protected_files(), []
        busy = bool(self._temp_busy())
        owners = list(self.jobs.values())
        jobs_root = self.data / 'jobs'
        # Validate the directory itself before enumerating possible orphan ids.
        try:
            safe_files(jobs_root, recursive=False)
        except (OSError, ValueError):
            return [{'id':'unmanaged', 'kind':'orphan', 'bytes':None, 'cleanable':False, 'error':'error.unsafe_cleanup'}]
        for root in jobs_root.iterdir() if jobs_root.exists() else []:
            if root.name in self.jobs or not re.fullmatch('[0-9a-f]{32}', root.name):
                continue
            row = {'id':'orphan:' + root.name, 'kind':'orphan', 'path':root, 'bytes':None, 'cleanable':False}
            try:
                configs, files = orphan_job(root, protected)
                owners.extend({**job, '_orphan':True} for job in configs)
                current_preview = self.preview_task and self.preview_task['id'] == root.name and self.preview_task['status'] == 'ready'
                row.update(bytes=sum(path.stat().st_size for path in files), cleanable=not busy and not current_preview,
                           name=Path(configs[-1]['source']).name,
                           partials=[self._partial_path(job) for job in configs])
            except (ValueError, OSError):
                row['error'] = 'error.unsafe_cleanup'
            rows.append(row)
        cache = self.data / 'engine-cache'
        row = {'id':'cache', 'kind':'cache', 'path':cache, 'bytes':None, 'cleanable':False}
        try:
            files = safe_files(cache, protected)
            selected = [path for path in files if path.parent == cache and re.fullmatch(r'[0-9a-f]{8,64}\.engine(?:\.cache)?', path.name)]
            row.update(bytes=sum(path.stat().st_size for path in files), cleanable=bool(selected) and not busy, files=selected)
        except (OSError, ValueError):
            row['error'] = 'error.unsafe_cleanup'
        rows.append(row)
        seen = set()
        for job in owners:
            row = {'id':'partial:' + job['id'], 'kind':'partial', 'bytes':None, 'cleanable':False, 'owner':job}
            try:
                path = self._partial_path(job)
                identity = str(path).casefold()
                if identity in seen:
                    continue
                seen.add(identity)
                row['id'] += ':' + hashlib.sha256(identity.encode()).hexdigest()[:16]
                if not path.exists() and not path.is_symlink():
                    continue
                safe_files(path, protected)
                if not path.is_file() or path.stat().st_nlink > 1:
                    raise ValueError('error.unsafe_cleanup')
                row.update(path=path, name=job.get('name', Path(job['source']).name), bytes=path.stat().st_size,
                           cleanable=not busy and bool(job.get('_orphan') or job.get('status') in HISTORY | {'paused'}))
            except (OSError, ValueError):
                row['error'] = 'error.unsafe_cleanup'
            rows.append(row)
        return rows

    def cleanup_extra_files(self, identifier, confirmed=False):
        if confirmed is not True:
            raise ValueError('error.confirmation')
        with self.lock:
            if self._temp_busy():
                raise ValueError('error.temp_busy')
            row = next((row for row in self._storage_extras() if row['id'] == identifier), None)
            if not row or not row['cleanable']:
                raise ValueError('error.unsafe_cleanup')
            if row['kind'] == 'cache':
                for path in row['files']:
                    path.unlink()
            elif row['kind'] == 'orphan':
                if any(path.exists() for path in row['partials']):
                    raise ValueError('error.temp_partial_first')
                shutil.rmtree(row['path'])
            else:
                # Never infer ownership of a video in a user folder from its name alone.
                from borasuki.pipeline import probe
                before = row['path'].stat()
                try:
                    tags = probe(row['path'], self.runtime, threading.Event()).get('format', {}).get('tags', {})
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ValueError('error.temp_owner') from exc
                if not any(key.lower() == 'borasuki_job' and value == row['owner']['id'] for key,value in tags.items()):
                    raise ValueError('error.temp_owner')
                safe_files(row['path'], self._protected_files())
                after = row['path'].stat()
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError('error.unsafe_cleanup')
                row['path'].unlink()

    def cleanup_job_files(self, identifier, confirmed=False):
        self._cleanup_job_files(identifier, confirmed, all_revisions=True)

    def _cleanup_job_files(self, identifier, confirmed, all_revisions):
        if confirmed is not True:
            raise ValueError('error.confirmation')
        with self.lock:
            job = self.jobs[identifier]
            if identifier == self.active_id or job['status'] not in HISTORY | {'paused'}:
                raise ValueError('error.job_locked')
            work = self._owned_work({**job, 'work':str(self.data / 'jobs' / identifier)} if all_revisions else job)
            if all_revisions and self._partial_path(job).exists():
                raise ValueError('error.temp_partial_first')
            # Persist invalidation before any deletion.
            self.update(identifier, resume_deleted=True, resume_deleted_at=time.time())
            if work.exists():
                shutil.rmtree(work)

    def expire_resume(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if not self.settings["retention_consent"] or self.settings["resume_retention"] != 30:
                return
            for job in list(self.jobs.values()):
                if (job["status"] == "paused" and job["id"] != self.active_id and not job.get("resume_deleted")
                        and now >= job["paused_at"] + 30 * 86400):
                    try:
                        self.cleanup_resume(job["id"], confirmed=True)
                    except (OSError, ValueError):
                        logger.exception("Resume cleanup failed id=%s", job["id"])

    def update(self, identifier: str, persist=True, **fields):
        with self.lock:
            old = self.jobs[identifier]
            if "configuration" in fields or any(key in fields for key in CONFIG_KEYS if key not in ("tile", "analysis")):
                raise ValueError("error.job_locked")
            if fields.get("status") == "running" and old["status"] in ("pause_requested", "cancel_requested"):
                fields.pop("status")
            updated = {**old, **fields}
            if persist:
                self._commit(updated, runtime_update=True)
            else:
                self.jobs[identifier] = updated

    def snapshot(self) -> dict:
        if self.active_id is not None and self.settings["metrics"]["vram"] and time.monotonic() - self.memory_sample_at >= 3:
            self.memory_sample_at = time.monotonic()
            self.memory_sample = gpu_memory()
        with self.lock:
            jobs = copy.deepcopy(sorted(self.jobs.values(), key=lambda j: j["order"]))
            for job in jobs:
                if job.get("error") and not job.get("failure"):
                    job["failure"] = failure_info(RuntimeError(job["error"]))
                job["locked"] = job["id"] == self.active_id or job["status"] in ACTIVE
                job["actions"] = self.available_actions(job, job["locked"])
                if job["id"] == self.active_id and self.active_started is not None:
                    job["elapsed"] += time.monotonic() - self.active_started
                    job["vram"] = self.memory_sample.get(job["gpu_id"])
                if job["status"] == "paused" and not job.get("resume_deleted") and self.settings["resume_retention"] == 30 and self.settings["retention_consent"]:
                    job["resume_expires"] = job["paused_at"] + 30 * 86400
            task = self.preview_task
            preview = {key: copy.deepcopy(task[key]) for key in ('id', 'status', 'stage', 'progress', 'frames', 'total_frames', 'start', 'duration', 'error', 'failure', 'fps', 'width', 'height', 'start_frame') if key in task} if task else None
            if preview and task.get('job'):
                preview['configuration'] = copy.deepcopy(task['job']['configuration'])
                preview['preflight'] = copy.deepcopy(task['job']['preflight'])
            analysis = {key: copy.deepcopy(value) for key, value in self.analysis_task.items()
                        if key not in ("stop", "future", "source_signature")} if self.analysis_task else None
            if self.engine_task:
                cached = preparation.ready(self.engine_task['config'])
                if self.engine_task['status'] in ('waiting', 'ready') and cached:
                    self.engine_task.update(status='ready', error=None, failure=None)
                elif self.engine_task['status'] == 'ready' and not cached:
                    failure = failure_info(ValueError('error.engine_preparation_required'))
                    self.engine_task.update(status='failed', error=failure['code'], failure=failure)
            return {"runtime": copy.deepcopy(self.runtime), "settings": copy.deepcopy(self.settings), "jobs": jobs,
                    'updates': self.updates.snapshot(),
                    'preparation': {key: copy.deepcopy(value) for key, value in self.engine_task.items()
                                    if key not in ('config', 'stop')} if self.engine_task else None,
                    "preview": preview, "analysis": analysis,
                    "batch": {key: copy.deepcopy(value) for key, value in self.batch_task.items() if key not in ('stop', 'future')} if self.batch_task else None,
                    "setup": {key: copy.deepcopy(value) for key, value in self.setup.items() if key != "signature"}}

    def export_diagnostics(self, folder):
        from borasuki.diagnostics import export
        return export(self.snapshot(), self.data, folder, self._owned_work)

    def save_settings(self, values: dict, confirmed=False):
        with self.lock:
            updated = dict(self.settings)
            for key in ("language", "theme", "output_folder", "sound", "notifications", "context_menu", "default_gpu", "metrics", "resume_retention", "queue_enabled"):
                if key in values:
                    value = values[key]
                    if key == "language" and value not in ("tr", "en") or key == "theme" and value not in ("dark", "light"):
                        raise ValueError("error.invalid_settings")
                    if key == "output_folder" and not isinstance(value, str) or key in ("sound", "notifications", "context_menu", "queue_enabled") and type(value) is not bool:
                        raise ValueError("error.invalid_settings")
                    if key == "default_gpu" and (type(value) is not int or value not in [g["id"] for g in self.runtime["gpus"]]):
                        raise ValueError("error.unsupported_gpu")
                    if key == "metrics" and (not isinstance(value, dict) or set(value) != set(METRICS) or any(type(v) is not bool for v in value.values())):
                        raise ValueError("error.invalid_settings")
                    if key == "resume_retention":
                        if value != "never" and (type(value) is not int or value != 30):
                            raise ValueError("error.invalid_settings")
                        if value == 30 and not updated["retention_consent"]:
                            if confirmed is not True:
                                raise ValueError("error.confirmation")
                            updated["retention_consent"] = True
                    updated[key] = value
            if "context_menu" in values or "language" in values and updated["context_menu"]:
                from borasuki.windows import context_menu
                context_menu(updated["context_menu"], updated["language"])
            atomic_json(self.data / "settings.json", updated)
            self.settings = updated
        self.wake.set()

    def _loop(self):
        last_cleanup = 0
        while not self.closing:
            if time.monotonic() - last_cleanup > 60:
                self.expire_resume()
                last_cleanup = time.monotonic()
            with self.lock:
                if self.closing:
                    break
                setup = self.setup["status"] == "waiting"
                if setup:
                    self.setup["status"] = "checking"
                    self.stop = threading.Event()
                ready = self.setup["status"] == "ready"
                task = self.preview_task if ready and self.preview_task and self.preview_task['status'] == 'waiting' else None
                if task:
                    task['status'] = 'preparing'
                job = next((j for j in sorted(self.jobs.values(), key=lambda j: j["order"]) if j["status"] == "queued"), None) if ready and self.settings["queue_enabled"] and not task else None
                if job:
                    self.active_id = job["id"]
                    self.stop = threading.Event()
                    self.active_started = time.monotonic()
                    job = copy.deepcopy({**job, **job.get("configuration", {})})
                engine = self.engine_task if ready and not task and not job and self.engine_task and self.engine_task['status'] == 'waiting' else None
                if engine:
                    engine['status'] = 'preparing'
            if setup:
                self._run_setup()
                continue
            if task:
                self._run_preview(task)
                continue
            if not job:
                if engine:
                    self._run_preparation(engine)
                    continue
                self.wake.wait(1)
                self.wake.clear()
                continue
            identifier = job["id"]
            try:
                self.update(identifier, status="running", stage="preflight", speed=None, eta=None)
                current_runtime = discover()
                with self.lock:
                    self.runtime = current_runtime
                self._require_setup(current_runtime)
                job['runtime'] = current_runtime
                if not preparation.ready(job):
                    self.update(identifier, stage="engine_preparing", speed=None, eta=None)
                    preparation.prepare(job, self.data, self.stop,
                                        update=lambda **fields: self.update(identifier, persist=False, **fields))
                preparation.require(job)
                job['require_engine_prepared'] = True
                self.update(identifier, stage="preflight", speed=None, eta=None)
                checks = preflight(job, current_runtime, allow_published=job.get("recover_published", False))
                self.update(identifier, preflight=checks, warning=checks["warning"])
                if self.stop.is_set():
                    raise Interrupted()
                execute(job, self.stop, lambda **fields: self.update(identifier, **fields))
                if self.settings.get("notifications"):
                    try:
                        from borasuki.windows import notify
                        notify(Path(job["output"]).name, self.settings["language"], self.data)
                    except Exception:
                        logger.exception("Windows notification failed id=%s", identifier)
                if self.settings.get("sound"):
                    try:
                        import winsound
                        winsound.MessageBeep(winsound.MB_OK)
                    except Exception:
                        logger.exception("Completion sound failed id=%s", identifier)
            except Interrupted:
                with self.lock:
                    status = self.jobs[identifier]["status"]
                    final = "cancelled" if status == "cancel_requested" else "paused" if status == "pause_requested" or not self.closing else "queued"
                    self.update(identifier, status=final, paused_at=time.time() if final == "paused" else self.jobs[identifier].get("paused_at"),
                                recover_published=final == "queued", speed=None, eta=None)
            except Exception as exc:
                logger.exception("Job failed id=%s source=%s", identifier, job["source"])
                failure = failure_info(exc)
                with self.lock:
                    status = self.jobs[identifier]["status"]
                    final = "cancelled" if status == "cancel_requested" else "paused" if status == "pause_requested" else "queued" if self.closing else "failed"
                    self.update(identifier, status=final, recover_published=final == 'queued',
                                paused_at=time.time() if status == "pause_requested" else self.jobs[identifier].get("paused_at"),
                                error=failure["code"] if final == 'failed' else None,
                                failure=failure if final == 'failed' else None, speed=None, eta=None)
            finally:
                with self.lock:
                    current = self.jobs[identifier]
                    fields = {"elapsed": current["elapsed"] + time.monotonic() - self.active_started}
                    if current["status"] in HISTORY:
                        fields["finished"] = time.time()
                    self.update(identifier, **fields)
                    self.active_id = None
                    self.active_started = None

    def close_context(self):
        with self.lock:
            return {'busy': bool(self.active_id or self.setup['status'] in ('waiting', 'checking')
                    or self.engine_task and self.engine_task['status'] in ('waiting', 'preparing')
                    or self.preview_task and self.preview_task['status'] in ('waiting', 'preparing', 'rendering')
                    or self.analysis_task and self.analysis_task['status'] in ('waiting', 'analyzing')
                    or self.batch_task and self.batch_task['status'] in ('waiting', 'preparing', 'stopping')),
                    'closing': self.closing}

    def begin_shutdown(self, confirmed=False):
        with self.lock:
            if not self.closing and self.close_context()['busy'] and confirmed is not True:
                raise ValueError('error.confirmation')
            self.closing = True
            self.stop.set()
            if self.engine_task:
                self.cancel_preparation(self.engine_task['id'])
            if self.batch_task:
                self.cancel_batch(self.batch_task['id'])
            if self.preview_task:
                self.preview_task['stop'].set()
                if self.preview_task['status'] == 'waiting':
                    self.preview_task['status'] = 'cancelled'
            self._cancel_analysis()
        self.wake.set()

    def shutdown(self, timeout=10):
        self.updates.close()
        self.begin_shutdown(confirmed=True)
        if self.source_server:
            self.source_server.close()
        deadline = time.monotonic() + timeout
        self.analysis_pool.shutdown(wait=False, cancel_futures=True)
        if self.worker.is_alive():
            self.worker.join(timeout=max(0, deadline - time.monotonic()))
        with self.lock:
            futures = tuple(self.analysis_futures)
        for future in futures:
            try:
                future.result(timeout=max(0, deadline - time.monotonic()))
            except CancelledError:
                pass
            except TimeoutError as exc:
                logger.warning('Shutdown is waiting for color analysis')
                raise TimeoutError('error.shutdown_timeout') from exc
        if self.worker.is_alive():
            logger.warning('Shutdown is waiting for render/setup worker id=%s', self.active_id)
            raise TimeoutError('error.shutdown_timeout')
