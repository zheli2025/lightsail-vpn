import hashlib
import importlib.util
import json
import subprocess
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
    def test_email_is_optional_but_supplied_email_is_validated(self):
        args=boot.parser().parse_args(['--ip','1.1.1.1','--node','节点','--region','ap-northeast-1',
                                      '--instance-name','vpn','--accept-tos'])
        self.assertIsNone(args.email)
        boot.validate(args)
        command=boot.certbot_command(args)
        self.assertIn('--register-unsafely-without-email',command)
        self.assertNotIn('--email',command)
        args.email='admin@example.com'
        boot.validate(args)
        command=boot.certbot_command(args)
        self.assertIn('--email',command)
        self.assertNotIn('--register-unsafely-without-email',command)
        args.email='not an email'
        with self.assertRaises(ValueError): boot.validate(args)

    def test_certificate_reuse_checks_public_ip_expiry_and_renewability(self):
        with tempfile.TemporaryDirectory() as tmp:
            live=Path(tmp)/'live'; directory=live/'1.1.1.1'; directory.mkdir(parents=True)
            (directory/'fullchain.pem').write_text('public certificate fixture')
            (directory/'privkey.pem').write_text('private fixture; do not read')
            calls=[]
            self.assertFalse(boot.reusable_certificate('1.1.1.1',live,calls.append))
            (Path(tmp)/'renewal').mkdir(); (Path(tmp)/'renewal/1.1.1.1.conf').touch()
            self.assertTrue(boot.reusable_certificate('1.1.1.1',live,calls.append))
            self.assertTrue(any('-checkip' in c for c in calls))
            self.assertTrue(any('-checkend' in c for c in calls))
            self.assertFalse(any('privkey.pem' in ' '.join(c) for c in calls))
            def invalid(command): raise subprocess.CalledProcessError(1,command)
            self.assertFalse(boot.reusable_certificate('1.1.1.1',live,invalid))

    def test_server_software_baseline(self):
        baseline=json.loads((ROOT/'config/software-baseline.json').read_text())['hysteria']
        # 这里只解析生成的假凭据配置，不访问生产配置。
        text=boot.configuration('fixture-auth','fixture-obfs','fixture-stats')
        roots=[line.split(':',1)[0] for line in text.splitlines() if line and not line.startswith(' ')]
        self.assertEqual(set(roots),set(baseline))
        self.assertIn('listen: '+baseline['listen'],text)
        self.assertIn('  listen: '+baseline['trafficStats']['listen'],text)
        self.assertIn('  type: '+baseline['auth']['type'],text)
        self.assertIn('  type: '+baseline['obfs']['type'],text)

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
                args.email=None
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
