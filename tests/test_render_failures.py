"""Real child-process failures without GPU inference."""

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from borasuki.errors import RenderDiagnostics, failure_info
from borasuki.pipeline import render_segment
from borasuki.process import Interrupted, ProcessError, ProcessGroup
from borasuki.storage import atomic_json


class RenderFailureTests(unittest.TestCase):
    def test_recovery_routes_are_specific_and_keep_cause(self):
        for code, route in [('error.disk_space', 'storage'), ('error.output_exists', 'output'),
                            ('error.denoise_model_missing', 'setup'), ('error.setup_encoding', 'setup'),
                            ('error.video_seek', 'source')]:
            failure = failure_info(ValueError(code))
            self.assertEqual(failure['recovery'], route)
            self.assertEqual(failure['detail'], code)
        self.assertEqual(failure_info(PermissionError('blocked'))['code'], 'error.file_access')

    def test_memory_error_survives_progress_flood(self):
        diagnostic = RenderDiagnostics()
        diagnostic.add('CUDA out of memory')
        for frame in range(500):
            diagnostic.add(f'frame={frame}')
        self.assertEqual(failure_info(ProcessError(diagnostic.detail()))['code'], 'error.gpu_memory')

    def run_children(self, producer, consumer, stop, **callbacks):
        original_spawn = ProcessGroup.spawn
        children = []

        def spawn(group, args, **kwargs):
            child = original_spawn(group, [sys.executable, '-c', producer if not children else consumer], **kwargs)
            children.append(child)
            return child

        try:
            with tempfile.TemporaryDirectory() as folder:
                work = Path(folder)
                atomic_json(work / 'config.json', {'matrix': 1})
                with patch.object(ProcessGroup, 'spawn', spawn):
                    render_segment(work / 'config.json', {'vspipe':'producer', 'ffmpeg':'encoder'},
                                   work / 'partial.mkv', 0, 64, stop, callbacks.pop('on_progress', lambda frame: None), work / 'render.log', **callbacks)
        finally:
            self.assertEqual(len(children), 2)
            self.assertTrue(all(child.poll() is not None for child in children), 'Orphaned render process')

    def test_producer_failure_is_not_success_when_encoder_exits_zero(self):
        producer = "import sys; print('Error: fwrite() call failed, frame: 38', file=sys.stderr); sys.exit(1)"
        consumer = "import sys; sys.stdin.buffer.read(); print('frame=38\\n'*100, file=sys.stderr)"
        with self.assertRaises(ProcessError) as caught:
            self.run_children(producer, consumer, threading.Event())
        self.assertIn('VSPipe exit: 1; FFmpeg exit: 0', str(caught.exception))
        self.assertEqual(failure_info(caught.exception)['code'], 'error.frame_transfer')

    def test_encoder_failure_stops_producer_and_retains_cause(self):
        producer = "import time; time.sleep(30)"
        consumer = "import sys; print('No space left on device', file=sys.stderr); sys.exit(1)"
        with self.assertRaises(ProcessError) as caught:
            self.run_children(producer, consumer, threading.Event())
        self.assertEqual(failure_info(caught.exception)['code'], 'error.disk_space')

    def test_cancel_stops_both_children(self):
        stop = threading.Event()
        timer = threading.Timer(1, stop.set)
        timer.start()
        try:
            with self.assertRaises(Interrupted):
                self.run_children('import time; time.sleep(30)', 'import sys; sys.stdin.buffer.read()', stop)
        finally:
            timer.cancel()

    def test_inference_progress_arrives_before_encoder_with_cr_lines(self):
        events = []
        producer = "import sys,time; print('BORASUKI_STAGE=engine_building',file=sys.stderr,flush=True); sys.stderr.write('Frame: 1/64\\rFrame: 2/64\\r'); sys.stderr.flush(); time.sleep(.2); sys.stdout.write('done')"
        consumer = "import sys; sys.stdin.buffer.read(); print('frame=1',file=sys.stderr,flush=True)"
        self.run_children(producer, consumer, threading.Event(), on_inference=lambda frame: events.append(('ai',frame)),
                          on_progress=lambda frame: events.append(('encode',frame)), on_stage=lambda stage: events.append(('stage',stage)))
        self.assertIn(('stage','engine_building'), events)
        self.assertIn(('ai',1), events)
        self.assertLess(events.index(('ai',1)), events.index(('encode',1)))


if __name__ == '__main__':
    unittest.main()
