import hashlib
import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def module(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file)
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


boot=module('bootstrap','bootstrap.py')
release=module('release','verify-release.py')


class TestParity(unittest.TestCase):
    def test_original_hashes_with_only_documented_fixes(self):
        for line in (ROOT/'baseline.sha256').read_text().splitlines():
            expected,file=line.split(maxsplit=1)
            data=(ROOT/file).read_bytes()
            if file=='scripts/domain-usage.py':
                text=data.decode().replace('        conn = connect_db()\n        updated_at = load_meta(conn).get("last_poll_ok")\n        conn.close()\n','')
                text=text.replace('"updated_at": updated_at,','"updated_at": load_meta(conn).get("last_poll_ok"),')
                text=text.replace('    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")','        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")')
                data=text.encode()
            self.assertEqual(hashlib.sha256(data).hexdigest(),expected,file)

    def test_bootstrap_reentry_preserves_secrets(self):
        args=boot.parser().parse_args(['--ip','1.1.1.1','--node','测试-01','--region','ap-northeast-1',
                                      '--instance-name','vpn-tokyo-01','--email','admin@example.com','--accept-tos'])
        boot.validate(args)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(boot,'MARKER',root/'install.json'), patch.object(boot,'BASE',root/'hysteria'), patch.object(boot,'TRAFFIC',root/'traffic.conf'), patch.object(boot.os,'chown'), patch.object(boot.grp,'getgrnam',return_value=types.SimpleNamespace(gr_gid=0)), patch.object(boot.pwd,'getpwnam',return_value=types.SimpleNamespace(pw_uid=0)):
                boot.initialize(args)
                before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
                boot.initialize(args)
                self.assertEqual(before,{p:p.read_bytes() for p in before})
                self.assertIn('Environment=LS_INSTANCE_NAME=vpn-tokyo-01',(root/'traffic.conf').read_text())
                self.assertIn('127.0.0.1:25413',(root/'hysteria/config.yaml').read_text())
                args.ip='8.8.8.8'
                with self.assertRaises(ValueError): boot.initialize(args)

    def test_release_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); name='hysteria-linux-amd64'
            (root/name).write_bytes(b'fixture')
            (root/'hashes.txt').write_text(hashlib.sha256(b'fixture').hexdigest()+'  build/'+name+'\n')
            release.verify(root,name)
            (root/name).write_bytes(b'corrupted')
            with self.assertRaises(ValueError): release.verify(root,name)

    def test_services_and_schedule(self):
        unit=(ROOT/'systemd/hysteria-server.service').read_text()
        self.assertIn('RestartSec=3',unit); self.assertIn('LimitNOFILE=1048576',unit)
        self.assertIn('00,06,12,18:00:00 Asia/Shanghai',(ROOT/'systemd/geo-update.timer').read_text())
        self.assertIn('OnCalendar=*:0/30',(ROOT/'systemd/traffic-update.timer').read_text())
        self.assertIn('--days 180',(ROOT/'systemd/domain-usage-prune.service').read_text())
