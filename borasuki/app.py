"""Thin pywebview bridge; rendering remains outside the UI process."""

import functools
import json
import logging
import os
import sys
import threading
from pathlib import Path

import webview
from webview.dom import DOMEventHandler

from borasuki.i18n import load_catalog
from borasuki.errors import failure_info
from borasuki.service import Service
from borasuki.batch import collect as collect_videos
from borasuki.storage import DATA, ROOT

logger = logging.getLogger(__name__)


def endpoint(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        api = args[0]
        tracked = False
        try:
            with api._commands:
                if api._service.closing and function.__name__ not in ('state', 'close_context', 'close_app', 'open_logs', 'export_diagnostics'):
                    raise ValueError('error.app_closing')
                tracked = function.__name__ not in ('state', 'close_context', 'close_app')
                if tracked:
                    api._pending_commands += 1
            return {"ok": True, "data": function(*args, **kwargs)}
        except Exception as exc:
            logger.exception("API %s failed", function.__name__)
            failure = failure_info(exc)
            return {"ok": False, "error": failure["code"], "detail": failure["detail"], "failure": failure}
        finally:
            if tracked:
                with api._commands:
                    api._pending_commands -= 1
                    api._commands.notify_all()
    return call


class API:
    def __init__(self, service: Service):
        self._service = service
        self._window = None
        self._listener = None
        self._commands = threading.Condition(service.lock)
        self._pending_commands = 0
        self._close_lock = threading.Lock()
        self._close_thread = None
        self._closed_safely = False
        self._import_lock = threading.Lock()

    @endpoint
    def close_context(self):
        return self._service.close_context()

    @endpoint
    def close_app(self, confirmed=False):
        with self._close_lock:
            if self._close_thread and self._close_thread.is_alive():
                return
            self._service.begin_shutdown(confirmed)
            self._close_thread = threading.Thread(target=self._finish_close, name='desktop-close', daemon=True)
            self._close_thread.start()

    def _finish_close(self):
        try:
            self._service.shutdown()
            with self._commands:
                if not self._commands.wait_for(lambda: self._pending_commands == 0, timeout=10):
                    logger.warning('Shutdown is waiting for %s desktop commands', self._pending_commands)
                    raise TimeoutError('error.shutdown_timeout')
        except Exception as exc:
            logger.exception('Desktop close could not finish safely')
            with self._close_lock:
                self._close_thread = None
            self._window.evaluate_js('window.closeFailed(' + json.dumps(failure_info(exc)) + ')')
            return
        self._closed_safely = True
        self._window.destroy()

    def _on_closing(self):
        if self._closed_safely:
            return True
        # Dispatch outside the native close event; never block its UI thread.
        threading.Thread(target=self._ask_close, name='desktop-close-prompt', daemon=True).start()
        return False

    def _ask_close(self):
        try:
            handled = self._window.evaluate_js("typeof window.requestAppClose === 'function' && (window.requestAppClose(), true)")
            if not handled:
                # Before the form is loaded there is no unsaved UI draft.
                self.close_app(confirmed=True)
        except Exception:
            logger.exception('Could not show close confirmation; window remains open')

    @endpoint
    def state(self):
        result = self._service.snapshot()
        result["catalog"] = load_catalog(result["settings"]["language"])
        return result

    @endpoint
    def client_ready(self):
        logger.info("Frontend connected to application service")

    @endpoint
    def check_updates(self):
        self._service.updates.request()

    @endpoint
    def open_update(self):
        result = self._service.updates.snapshot()
        if result['status'] == 'available':
            import webbrowser
            from borasuki.updates import REPOSITORY, version_key
            tag = 'v' + result['version']
            version_key(tag)
            webbrowser.open(f'{REPOSITORY}/releases/tag/{tag}')

    @endpoint
    def choose_file(self):
        selection = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
            file_types=("Video (*.mp4;*.mkv;*.avi;*.mov;*.webm;*.m4v)",))
        if not selection:
            return None
        try:
            return self._service.inspect(selection[0])
        except Exception as exc:
            logger.exception("File import failed source=%s", selection[0])
            return {"import_error": failure_info(exc), "path": selection[0]}

    @endpoint
    def choose_folder(self):
        selection = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return selection[0] if selection else None

    @endpoint
    def choose_batch(self, folder=False):
        selection = self._window.create_file_dialog(webview.FileDialog.FOLDER) if folder else self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True, file_types=('Video (*.mp4;*.mkv;*.avi;*.mov;*.webm;*.m4v)',))
        return collect_videos(selection) if selection else None

    @endpoint
    def inspect_file(self, source):
        try:
            return self._service.inspect(source)
        except Exception as exc:
            logger.exception('File import failed source=%s', source)
            return {'import_error': failure_info(exc), 'path': source}

    @endpoint
    def request_batch(self, paths, output_folder, values, gpu_id, preset=None, output_format='mkv', drop_tracks_confirmed=False):
        return self._service.request_batch(paths, output_folder, values, gpu_id, preset, output_format, drop_tracks_confirmed)

    @endpoint
    def cancel_batch(self, token):
        self._service.cancel_batch(token)

    @endpoint
    def create_job(self, source, output, color_mode, gpu_id, custom, upscale, denoise, adaptive=None, preset=None, drop_tracks_confirmed=False):
        return self._service.create(source, output, color_mode, gpu_id, custom, upscale, denoise, adaptive, preset, drop_tracks_confirmed)

    @endpoint
    def list_presets(self):
        return self._service.list_presets()

    @endpoint
    def save_preset(self, name, values):
        return self._service.save_preset(name, values)

    @endpoint
    def delete_preset(self, identifier, confirmed=False):
        self._service.delete_preset(identifier, confirmed)

    @endpoint
    def request_analysis(self, source, editing=None):
        return self._service.request_analysis(source, editing)

    @endpoint
    def cancel_analysis(self, token):
        self._service.cancel_analysis(token)

    @endpoint
    def check_setup(self):
        self._service.request_setup()

    @endpoint
    def request_preparation(self, source, upscale, denoise, gpu_id):
        return self._service.request_preparation(source, upscale, denoise, gpu_id)

    @endpoint
    def cancel_preparation(self, token):
        self._service.cancel_preparation(token)

    @endpoint
    def check_draft(self, values, editing=None, saved=False):
        return self._service.check_draft(values, editing, saved)

    @endpoint
    def request_preview(self, values, start=0, duration=3, editing=None, saved=False):
        return self._service.request_preview(values, start, duration, editing, saved)

    @endpoint
    def cancel_preview(self, token):
        self._service.cancel_preview(token)

    @endpoint
    def preview_media(self, token):
        return self._service.preview_media(token)

    @endpoint
    def source_media(self, source):
        return self._service.source_media(source)

    @endpoint
    def commit_preview(self, token):
        return self._service.commit_preview(token)

    @endpoint
    def job_action(self, identifier, action, confirmed=False):
        self._service.action(identifier, action, confirmed)

    @endpoint
    def begin_edit(self, identifier):
        return self._service.begin_edit(identifier)

    @endpoint
    def end_edit(self, identifier, token, values=None):
        self._service.end_edit(identifier, token, values)

    @endpoint
    def reorder(self, identifiers):
        self._service.reorder(identifiers)

    @endpoint
    def move_job(self, identifier, direction):
        self._service.move(identifier, direction)

    @endpoint
    def remove_jobs(self, scope, identifier=None, confirmed=False):
        return self._service.remove(scope, identifier, confirmed)

    @endpoint
    def cleanup_resume(self, identifier, confirmed=False):
        self._service.cleanup_resume(identifier, confirmed)

    @endpoint
    def storage_usage(self):
        return self._service.storage_usage()

    @endpoint
    def cleanup_job_files(self, identifier, confirmed=False):
        self._service.cleanup_job_files(identifier, confirmed)

    @endpoint
    def cleanup_extra_files(self, identifier, confirmed=False):
        self._service.cleanup_extra_files(identifier, confirmed)

    @endpoint
    def settings(self, values, confirmed=False):
        self._service.save_settings(values, confirmed)

    @endpoint
    def open_output(self, identifier):
        job = self._service.snapshot()["jobs"]
        entry = next(j for j in job if j["id"] == identifier)
        folder = Path(entry["output"]).parent
        if folder.is_dir():
            os.startfile(str(folder))

    @endpoint
    def open_logs(self):
        os.startfile(str(self._service.data))

    @endpoint
    def export_diagnostics(self):
        selection = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return self._service.export_diagnostics(selection[0]) if selection else None

    @endpoint
    def _drop(self, event):
        selected = event.get("dataTransfer", {}).get("files", [])
        if not selected:
            return
        if not self._import_lock.acquire(blocking=False):
            return
        begun = False
        try:
            # Reserve the form before probing; active Queue work is independent.
            begun = self._window.evaluate_js('window.beginFileImport()') is True
            if not begun:
                return
            try:
                paths = [item.get('pywebviewFullPath') for item in selected]
                if any(not isinstance(path, str) or not Path(path).is_absolute() for path in paths):
                    raise ValueError('error.drop_path')
                logger.info('Drop import started count=%s', len(paths))
                if len(paths) > 1 or Path(paths[0]).is_dir():
                    result = {'batch': collect_videos(paths)}
                else:
                    result = {'media': self._service.inspect(paths[0])}
            except Exception as exc:
                logger.exception('Drop import failed files=%s', selected)
                result = {'failure': failure_info(exc, 'error.source_access')}
                if len(selected) == 1:
                    result['path'] = selected[0].get('pywebviewFullPath') or selected[0].get('name', '')
            self._window.evaluate_js('window.finishFileImport(' + json.dumps(result) + ')')
        finally:
            self._import_lock.release()

    def _loaded(self):
        self._window.show()
        self._service.updates.request()
        document = self._window.dom.document
        # Local JS handles hover feedback; only the real drop crosses the bridge.
        document.events.drop += DOMEventHandler(self._drop, True, True)
        if self._listener is None:
            from borasuki.windows import start_listener
            def imported(source):
                self._window.restore()
                self._window.show()
                if source:
                    self._drop({"dataTransfer": {"files": [{"pywebviewFullPath": source}]}})
            self._listener = start_listener(imported)
        logger.info("Desktop bridge and drag/drop ready")
        if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
            self._drop({"dataTransfer": {"files": [{"pywebviewFullPath": sys.argv[1]}]}})


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(DATA / "borasuki.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")],
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # one scheduler owns the database
    import msvcrt
    instance = (DATA / "instance.lock").open("a+b")
    instance.seek(0)
    try:
        msvcrt.locking(instance.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        from borasuki.windows import send_to_existing
        send_to_existing(sys.argv[1] if len(sys.argv) > 1 else None)
        return
    service = Service()
    api = API(service)
    window = webview.create_window("Borasuki", str(ROOT / "frontend/index.html"), js_api=api,
                                   width=1120, height=820, min_size=(860, 640), background_color="#061225")
    api._window = window
    window.events.loaded += api._loaded
    window.events.closing += api._on_closing
    try:
        webview.start(gui="edgechromium", debug=False)
    finally:
        service.shutdown()
        if api._listener:
            api._listener.close()
        instance.close()
