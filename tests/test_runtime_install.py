import io
import stat
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from borasuki.process import Interrupted
from borasuki.runtime_install import extract_zip


def archive(name, data=b'content', mode=stat.S_IFREG):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as zip_file:
        member = zipfile.ZipInfo(name)
        member.external_attr = (mode | 0o644) << 16
        zip_file.writestr(member, data)
    return io.BytesIO(output.getvalue())


class RuntimeInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stop = threading.Event()

    def test_extracts_only_inside_stage(self):
        extract_zip(archive('numpy/core.py'), self.root / 'stage', self.stop)
        self.assertEqual((self.root / 'stage/numpy/core.py').read_bytes(), b'content')

    def test_rejects_traversal_and_links(self):
        for name, mode in (('../outside', stat.S_IFREG), ('/outside', stat.S_IFREG),
                           ('C:/outside', stat.S_IFREG), ('link', stat.S_IFLNK)):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, 'error.package_archive'):
                    extract_zip(archive(name, mode=mode), self.root / 'stage', self.stop)
        self.assertFalse((self.root / 'outside').exists())

    def test_cancel_does_not_publish_next_member(self):
        self.stop.set()
        with self.assertRaises(Interrupted):
            extract_zip(archive('blocked'), self.root / 'stage', self.stop)
        self.assertFalse((self.root / 'stage/blocked').exists())
