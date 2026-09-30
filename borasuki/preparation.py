"""Reusable inference preparation receipts, independent of source/color/output."""

import hashlib
import json
import logging
from pathlib import Path
from fractions import Fraction

from borasuki.pipeline import script_args, check_hardware_encoder
from borasuki.process import Interrupted, ProcessGroup
from borasuki.profile import cugan_options
from borasuki.runtime import validation_signature
from borasuki.storage import ROOT, atomic_json
from borasuki.wait_progress import WaitProgress

logger = logging.getLogger(__name__)


def identity(config):
    script = Path(config.get('render_script') or ROOT / 'borasuki/render.vpy').resolve()
    signature = {'version': 1, 'runtime': validation_signature(config['runtime']),
                 'script': [script.stat().st_size, script.stat().st_mtime_ns],
                 'gpu': config['gpu_id'], 'cugan': cugan_options(config),
                 'size': [config['media']['width'], config['media']['height']],
                 'encoding': config.get('encoding'),
                 'fps': [config['media'].get('fps_num', 24), config['media'].get('fps_den', 1)],
                 'graph': config.get('trt_cuda_graph', False), 'streams': config.get('trt_streams', 1)}
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    return key, signature


def receipt_path(config):
    return Path(config['cache']) / 'prepared' / (identity(config)[0] + '.json')


def ready(config):
    try:
        receipt = json.loads(receipt_path(config).read_text(encoding='utf-8'))
        if receipt['signature'] != identity(config)[1] or not receipt['engines']:
            return False
        cache = Path(config['cache']).resolve()
        for name, size, modified in receipt['engines']:
            path = Path(name).resolve()
            if not path.is_relative_to(cache) or path.suffix != '.engine' or size < 1024:
                return False
            stat = path.stat()
            if [stat.st_size, stat.st_mtime_ns] != [size, modified]:
                return False
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning('Inference preparation receipt is invalid', exc_info=True)
        return False


def require(config):
    if not ready(config):
        raise ValueError('error.engine_preparation_required')


def prepare(config, data, stop, update=None):
    if ready(config):
        return
    key, signature = identity(config)
    work = Path(data).resolve() / 'preparation' / key
    work.mkdir(parents=True, exist_ok=True)
    # Script receipts can change without changing the engine workload.
    timing_key = hashlib.sha256(json.dumps({k: v for k, v in signature.items() if k != 'script'}, sort_keys=True).encode()).hexdigest()
    progress = WaitProgress(update or (lambda **fields: None), Path(data) / 'preparation' / (timing_key + '-timing.json'))
    telemetry = work / 'wait-progress.json'
    telemetry.unlink(missing_ok=True)
    progress.set('encoder_check')
    path = work / 'config.json'
    atomic_json(path, {**config, 'root': str(ROOT), 'work': str(work),
                       'grade': {'contrast': 1.0, 'brightness': 0.0, 'saturation': 1.0}})
    check_hardware_encoder(config, work, config['media']['width'] * 2, config['media']['height'] * 2,
                           Fraction(config['media'].get('fps_num', 24), config['media'].get('fps_den', 1)), stop)
    progress.set('engine_loading')
    # --info still evaluates the script; prepare mode also validates one frame.
    with ProcessGroup(stop) as group:
        try:
            group.capture(script_args(path, config['runtime'], 'prepare', 0, 1,
                                      config.get('render_script')), timeout=1800, on_poll=lambda: progress.poll(telemetry))
        except Interrupted:
            raise
        except Exception:
            logger.exception('Inference preparation failed key=%s work=%s', key, work)
            raise
    progress.poll(telemetry, force=True)
    if stop.is_set():
        raise Interrupted()
    if identity(config)[1] != signature:
        raise ValueError('error.runtime_changed')
    engines = json.loads((work / 'prepared-engines.json').read_text(encoding='utf-8'))
    progress.set('verification')
    atomic_json(receipt_path(config), {'signature': signature, 'engines': engines})
    require(config)
    if stop.is_set():
        raise Interrupted()
    progress.finish()
