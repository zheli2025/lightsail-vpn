import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('geo',Path(__file__).parents[1]/'scripts/geo-update.py')
geo=importlib.util.module_from_spec(spec)
spec.loader.exec_module(geo)


class TestGeo(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(patch.object(geo,'GEO',Path(tmp.name)))
        for name in geo.FILES: (geo.GEO/name).write_bytes(b'old-rules')

    def test_failed_download_preserves_old(self):
        with patch.object(geo.subprocess,'run',return_value=types.SimpleNamespace(returncode=1)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(RuntimeError): geo.update()
        for name in geo.FILES: self.assertEqual((geo.GEO/name).read_bytes(),b'old-rules')
        self.assertEqual(list(geo.GEO.glob('.download-*')),[])

    def test_validated_download_publishes(self):
        data=bytes.fromhex('28b52ffd')+b'test-fixture'*1024
        def fake(args,**kwargs):
            if args[0]=='curl': Path(args[args.index('-o')+1]).write_bytes(data)
            return types.SimpleNamespace(returncode=0)
        with patch.object(geo.subprocess,'run',side_effect=fake):
            with contextlib.redirect_stdout(io.StringIO()): geo.update()
        for name in geo.FILES:
            self.assertEqual((geo.GEO/name).read_bytes(),data)
            self.assertEqual((geo.GEO/name).stat().st_mode&0o777,0o644)

    def test_corrupt_stream_preserves_old(self):
        def fake(args,**kwargs):
            if args[0]=='curl':
                Path(args[args.index('-o')+1]).write_bytes(bytes.fromhex('28b52ffd')+b'bad'*1024)
                return types.SimpleNamespace(returncode=0)
            return types.SimpleNamespace(returncode=1)
        with patch.object(geo.subprocess,'run',side_effect=fake):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(RuntimeError): geo.update()
        for name in geo.FILES: self.assertEqual((geo.GEO/name).read_bytes(),b'old-rules')
