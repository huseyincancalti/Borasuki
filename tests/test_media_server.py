import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from borasuki.media_server import MediaServer, byte_range


class MediaServerTests(unittest.TestCase):
    def test_ranges_are_bounded_and_validated(self):
        self.assertEqual(byte_range(None, 10), (0, 9))
        self.assertEqual(byte_range('bytes=2-5', 10), (2, 5))
        self.assertEqual(byte_range('bytes=-3', 10), (7, 9))
        with self.assertRaises(ValueError):
            byte_range('bytes=10-', 10)
        with self.assertRaises(ValueError):
            byte_range('items=0-1', 10)

    def test_source_is_read_only_range_capable_and_revoked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.mp4'
            path.write_bytes(bytes(range(32)))
            server = MediaServer()
            self.addCleanup(server.close)
            url = server.grant(path)
            self.assertEqual(urlopen(url).read(), bytes(range(32)))
            response = urlopen(Request(url, headers={'Range':'bytes=4-7'}))
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), bytes(range(4, 8)))
            with self.assertRaises(HTTPError) as error:
                urlopen(url + '-wrong')
            self.assertEqual(error.exception.code, 404)
            path.write_bytes(b'changed')
            with self.assertRaises(HTTPError) as error:
                urlopen(url)
            self.assertEqual(error.exception.code, 409)
            server.close()
            with self.assertRaises((HTTPError, URLError)) as error:
                urlopen(url)
            if isinstance(error.exception, HTTPError):
                self.assertEqual(error.exception.code, 404)
