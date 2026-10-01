import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from borasuki.updates import ENDPOINT, UpdateCheck, fetch_update, version_key


class UpdateTests(unittest.TestCase):
    def fetch(self, releases, current='1.0.0-beta.3'):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(releases).encode()
        with patch('urllib.request.urlopen', return_value=response) as request:
            result = fetch_update(current)
        self.assertEqual(request.call_args.args[0].full_url, ENDPOINT)
        self.assertIsNone(request.call_args.args[0].data)
        self.assertEqual(request.call_args.kwargs['timeout'], 8)
        return result

    def test_semantic_order_not_lexical(self):
        tags = ['1.0.0-alpha.1', '1.0.0-beta.2', '1.0.0-beta.3', '1.0.0-beta.10',
                '1.0.0-rc.1', '1.0.0', '1.0.1-beta.1', '1.1.0', '2.0.0']
        self.assertEqual(sorted(reversed(tags), key=version_key), tags)

    def test_selects_newest_valid_release_and_ignores_drafts_and_remote_links(self):
        result = self.fetch([{'tag_name': 'v1.0.0-beta.2'}, {'tag_name': 'v9.0.0', 'draft': True},
                             {'tag_name': '<script>'}, {'tag_name': 'v1.0.0-beta.10', 'html_url': 'https://evil.test'}])
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['version'], '1.0.0-beta.10')
        self.assertEqual(result['url'], 'https://github.com/huseyincancalti/Borasuki/releases/tag/v1.0.0-beta.10')

    def test_stable_does_not_offer_beta_but_beta_can_move_to_stable(self):
        releases = [{'tag_name': 'v1.0.0'}, {'tag_name': 'v1.1.0-beta.1', 'prerelease': True}]
        self.assertEqual(self.fetch(releases, '1.0.0')['status'], 'up_to_date')
        self.assertEqual(self.fetch([releases[0]])['status'], 'available')

    def test_older_or_equal_is_not_an_update(self):
        for tag in ('v1.0.0-beta.2', 'v1.0.0-beta.3'):
            self.assertEqual(self.fetch([{'tag_name': tag}])['status'], 'up_to_date')

    def test_invalid_empty_and_oversized_response_do_not_claim_up_to_date(self):
        for payload in ([], {}, [{'tag_name': '../../bad'}]):
            with self.assertRaises(ValueError):
                self.fetch(payload)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b' ' * 1_048_577
        with patch('urllib.request.urlopen', return_value=response), self.assertRaises(ValueError):
            fetch_update()

    def test_background_check_is_nonblocking_and_coalesces_requests(self):
        started, release = threading.Event(), threading.Event()
        checker = UpdateCheck()
        def fetch():
            started.set()
            self.assertTrue(release.wait(2))
            return {'status': 'available', 'current': 'test', 'version': 'next'}
        with patch('borasuki.updates.fetch_update', side_effect=fetch) as request:
            checker.request()
            self.assertTrue(started.wait(1))
            checker.request()
            self.assertEqual(request.call_count, 1)
            self.assertEqual(checker.snapshot()['status'], 'checking')
            checker.close()
            release.set()
        self.assertTrue(checker.closed)

    def test_network_failure_is_recoverable_not_up_to_date(self):
        checker = UpdateCheck()
        with patch('borasuki.updates.fetch_update', side_effect=TimeoutError()), self.assertLogs('borasuki.updates'):
            checker._run()
        self.assertEqual(checker.snapshot()['status'], 'error')
        with patch('borasuki.updates.fetch_update', return_value={'status': 'up_to_date'}):
            checker._run()
        self.assertEqual(checker.snapshot()['status'], 'up_to_date')

    def test_bridge_opens_only_checked_canonical_release_and_no_installer(self):
        from borasuki.app import API
        service = MagicMock()
        service.lock = threading.RLock()
        service.closing = False
        api = API(service)
        service.updates.snapshot.return_value = {'status': 'available', 'version': '1.0.0-beta.4', 'url': 'https://evil.test'}
        with patch('webbrowser.open') as opened:
            self.assertTrue(api.open_update()['ok'])
            opened.assert_called_once_with('https://github.com/huseyincancalti/Borasuki/releases/tag/v1.0.0-beta.4')
        service.updates.snapshot.return_value = {'status': 'error'}
        with patch('webbrowser.open') as opened:
            self.assertTrue(api.open_update()['ok'])
            opened.assert_not_called()

    def test_application_installer_and_project_versions_agree(self):
        from borasuki.storage import ROOT
        from borasuki.version import VERSION
        import tomllib
        metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        self.assertEqual(metadata['project']['version'], VERSION.replace('-beta.', 'b'))
        self.assertIn(f'#define AppVersion "{VERSION}"', (ROOT / 'installer/Borasuki.iss').read_text())
