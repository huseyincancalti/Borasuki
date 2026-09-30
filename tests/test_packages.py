"""Transport faults must never publish an unverified runtime package."""

import hashlib
import io
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from borasuki.packages import PACKAGES, Package, _Redirects, _lock, download
from borasuki.process import Interrupted


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers if headers is not None else {'Content-Length': str(len(body))}
        self.url = 'https://release-assets.githubusercontent.com/package'


class PackageTests(unittest.TestCase):
    def test_release_manifest_has_pinned_runtime_components(self):
        required = {'python-3.13', 'numpy-2.5', 'vapoursynth-r79', 'cugan-v2', 'ffms2-5.0',
                    'ffmpeg-8.1', 'vsmlrt-scripts', 'vstrt', 'tensorrt-1', 'tensorrt-2', '7zr'}
        self.assertEqual(set(PACKAGES), required)
        self.assertGreater(sum(package.size for package in PACKAGES.values()), 2_800_000_000)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / 'packages'
        self.cache.mkdir()
        self.body = b'known package data' * 100
        self.package = Package('Test', 'https://github.com/owner/project/releases/download/v1/package.zip',
                               len(self.body), hashlib.sha256(self.body).hexdigest())
        self.partial = self.cache / (self.package.sha256 + '.partial')
        self.ready = self.cache / (self.package.sha256 + '.package')
        self.stop = threading.Event()
        self.opener = Mock()
        patcher = patch('borasuki.packages.urllib.request.build_opener', return_value=self.opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.updates = []

    def fetch(self, response=None, **kwargs):
        if response is not None:
            self.opener.open.return_value = response
        return download(self.package, self.cache, self.stop, lambda **fields: self.updates.append(fields), **kwargs)

    def test_download_hash_and_atomic_publication_then_verified_cache(self):
        def observe(**fields):
            self.updates.append(fields)
            if fields['stage'] != 'verified':
                self.assertFalse(self.ready.exists())
        self.opener.open.return_value = Response(self.body)
        self.assertEqual(download(self.package, self.cache, self.stop, observe), self.ready)
        self.assertEqual(self.ready.read_bytes(), self.body)
        self.assertFalse(self.partial.exists())
        self.assertEqual(self.fetch(), self.ready)
        self.opener.open.assert_called_once()
        self.assertEqual(self.updates[-1]['stage'], 'verified')

    def test_resumes_partial_only_with_exact_content_range(self):
        self.partial.write_bytes(self.body[:100])
        response = Response(self.body[100:], 206, {'Content-Range': f'bytes 100-{len(self.body)-1}/{len(self.body)}'})
        self.fetch(response)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Range'), 'bytes=100-')
        self.assertEqual(request.get_header('Accept-encoding'), 'identity')
        self.assertEqual(self.ready.read_bytes(), self.body)
        self.assertEqual(next(item['downloaded'] for item in self.updates if item['stage'] == 'downloading'), 100)

    def test_server_ignores_range_restart_does_not_append(self):
        self.partial.write_bytes(self.body[:99])
        self.fetch(Response(self.body))
        self.assertEqual(self.ready.read_bytes(), self.body)

    def test_bad_response_does_not_change_existing_partial(self):
        self.partial.write_bytes(self.body[:99])
        responses = [Response(b'data', 206, {'Content-Range': 'bytes 0-3/4'}),
                     Response(b'data', 206, {}), Response(self.body, 200, {'Content-Length': '4'}),
                     Response(self.body, 200, {'Content-Encoding': 'gzip'}), Response(b'data', 204)]
        for response in responses:
            with self.subTest(status=response.status, headers=response.headers):
                with self.assertRaisesRegex(ValueError, 'error.package_response'):
                    self.fetch(response)
                self.assertEqual(self.partial.read_bytes(), self.body[:99])
                self.assertFalse(self.ready.exists())

    def test_short_body_retained_for_retry_and_oversize_rejected(self):
        with self.assertRaisesRegex(ValueError, 'error.package_incomplete'):
            self.fetch(Response(self.body[:100], headers={}))
        self.assertEqual(self.partial.read_bytes(), self.body[:100])
        with self.assertRaisesRegex(ValueError, 'error.package_response'):
            self.fetch(Response(self.body + b'extra', headers={}))
        self.assertFalse(self.ready.exists())
        self.assertLessEqual(self.partial.stat().st_size, self.package.size)

    def test_hash_mismatch_is_quarantined_and_never_ready(self):
        with self.assertRaisesRegex(ValueError, 'error.package_integrity'):
            self.fetch(Response(b'x' * len(self.body)))
        self.assertFalse(self.ready.exists())
        self.assertFalse(self.partial.exists())
        rejected = list(self.cache.glob('*.rejected'))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].read_bytes(), b'x' * len(self.body))

    def test_complete_partial_after_crash_needs_no_network(self):
        self.partial.write_bytes(self.body)
        self.assertEqual(self.fetch(), self.ready)
        self.opener.open.assert_not_called()

    def test_corrupt_existing_cache_is_not_trusted_or_executed(self):
        self.ready.write_bytes(b'bad')
        self.fetch(Response(self.body))
        self.assertEqual(self.ready.read_bytes(), self.body)
        self.assertEqual(len(list(self.cache.glob('*.rejected'))), 1)

    def test_corrupt_or_oversized_complete_partial_restarts(self):
        for body in (b'?' * len(self.body), self.body + b'extra'):
            with self.subTest(size=len(body)):
                self.ready.unlink(missing_ok=True)
                self.partial.write_bytes(body)
                self.fetch(Response(self.body))
                self.assertIsNone(self.opener.open.call_args.args[0].get_header('Range'))
                self.assertEqual(self.ready.read_bytes(), self.body)

    def test_cancel_before_request_or_during_transfer_preserves_partial(self):
        self.stop.set()
        with self.assertRaises(Interrupted):
            self.fetch()
        self.opener.open.assert_not_called()
        self.stop.clear()
        self.opener.open.return_value = Response(self.body)
        def cancel(**fields):
            if fields['stage'] == 'downloading' and fields['downloaded']:
                self.stop.set()
        with self.assertRaises(Interrupted):
            download(self.package, self.cache, self.stop, cancel)
        self.assertFalse(self.ready.exists())
        self.assertEqual(self.partial.read_bytes(), self.body)

    def test_socket_failure_and_deadline_never_publish(self):
        self.opener.open.side_effect = urllib.error.URLError('connection lost')
        with self.assertRaisesRegex(ConnectionError, 'error.package_network'):
            self.fetch()
        self.opener.open.side_effect = TimeoutError('socket timeout')
        with self.assertRaisesRegex(TimeoutError, 'error.package_timeout'):
            self.fetch()
        self.opener.open.side_effect = None
        with patch('borasuki.packages.time.monotonic', side_effect=[1, 5]):
            with self.assertRaisesRegex(TimeoutError, 'error.package_timeout'):
                self.fetch(budget=1)
        self.assertFalse(self.ready.exists())

    def test_http_error_reports_status_without_signed_url(self):
        self.opener.open.side_effect = urllib.error.HTTPError('https://host/path?secret=token', 403, 'Denied', {}, None)
        with self.assertRaisesRegex(ValueError, 'error.package_http') as caught:
            self.fetch()
        self.assertEqual(caught.exception.__notes__, ['Test: HTTP 403'])
        self.assertNotIn('token', str(caught.exception))

    def test_low_space_preserves_partial_even_if_range_ignored(self):
        self.partial.write_bytes(self.body[:100])
        with patch('borasuki.packages.shutil.disk_usage', return_value=Mock(free=0)):
            with self.assertRaisesRegex(ValueError, 'error.disk_space'):
                self.fetch(Response(self.body))
        self.assertEqual(self.partial.read_bytes(), self.body[:100])

    def test_disk_write_error_never_publishes(self):
        with patch('borasuki.packages.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.fetch(Response(self.body))
        self.assertFalse(self.ready.exists())
        self.assertEqual(self.partial.read_bytes(), self.body)

    def test_atomic_publish_error_keeps_verified_partial(self):
        with patch('borasuki.packages.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                self.fetch(Response(self.body))
        self.assertFalse(self.ready.exists())
        self.assertEqual(self.partial.read_bytes(), self.body)

    def test_concurrent_same_package_is_rejected_without_writes(self):
        with _lock(self.cache / (self.package.sha256 + '.lock')):
            with self.assertRaisesRegex(ValueError, 'error.package_busy'):
                self.fetch()
        self.opener.open.assert_not_called()

    def test_hard_link_cache_file_does_not_overwrite_external_file(self):
        outside = self.root / 'keep.mp4'
        outside.write_bytes(b'keep source')
        os.link(outside, self.partial)
        with self.assertRaisesRegex(ValueError, 'error.package_path'):
            self.fetch()
        self.assertEqual(outside.read_bytes(), b'keep source')
        self.opener.open.assert_not_called()

    def test_invalid_manifest_or_insecure_redirect_is_rejected(self):
        for url in ('http://github.com/file', 'file:///C:/file', 'https://evil.example/file',
                    'https://github.com.evil.example/file', 'https://user:pass@github.com/file', 'https://github.com:8080/file'):
            with self.assertRaisesRegex(ValueError, 'error.package_source'):
                Package('Bad', url, 1, 'a' * 64)
            with self.assertRaisesRegex(ValueError, 'error.package_source'):
                _Redirects().redirect_request(urllib.request.Request(self.package.url), None, 302, 'Redirect', {}, url)
        for size, digest in ((0, 'a' * 64), (True, 'a' * 64), (1, 'not a hash')):
            with self.assertRaisesRegex(ValueError, 'error.package_manifest'):
                Package('Bad', self.package.url, size, digest)
        self.opener.open.assert_not_called()

    def test_unapproved_final_host_cannot_publish_even_with_valid_bytes(self):
        response = Response(self.body)
        response.url = 'https://unapproved.example/payload'
        with self.assertRaisesRegex(ValueError, 'error.package_source'):
            self.fetch(response)
        self.assertFalse(self.ready.exists())
        self.assertFalse(self.partial.exists())

    def test_interruption_while_reading_keeps_only_received_bytes(self):
        class BrokenResponse(Response):
            reads = 0
            def read1(self, size):
                self.reads += 1
                if self.reads > 1:
                    raise ConnectionResetError('connection reset')
                return super().read1(100)
        with self.assertRaisesRegex(ConnectionError, 'error.package_network') as caught:
            self.fetch(BrokenResponse(self.body))
        self.assertEqual(caught.exception.__notes__, ['Test: ConnectionResetError'])
        self.assertEqual(self.partial.read_bytes(), self.body[:100])
        self.assertFalse(self.ready.exists())

    def test_junction_or_symlink_cache_is_rejected(self):
        info = self.cache.lstat()
        linked = Mock(st_mode=info.st_mode, st_file_attributes=0x400, st_nlink=1)
        with patch.object(Path, 'lstat', return_value=linked):
            with self.assertRaisesRegex(ValueError, 'error.package_path'):
                self.fetch()
        self.opener.open.assert_not_called()
