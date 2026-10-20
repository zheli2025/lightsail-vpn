import contextlib
import importlib.util
import io
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('updater',ROOT/'scripts/update.py')
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)


class TestUpdate(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.calls=[]; self.active={'domain-usage.service','hysteria-server.service'}
        self.fail_restart=False
        self.update=mod.Updater(root=self.root,repo=ROOT,runner=self.run_command)
        for path in mod.REQUIRED: self.write(path,b'private fixture, must remain untouched')
        self.write('/etc/systemd/system/traffic-update.service.d/migrate.conf',b'[Service]\nEnvironment=LS_REGION=ap-northeast-1\nEnvironment=LS_INSTANCE_NAME=vpn-tokyo\nEnvironment=LS_PUBLIC_IP=1.1.1.1\n')
        for unit in mod.UNITS:
            self.write('/etc/systemd/system/'+unit,(ROOT/'systemd'/unit).read_bytes())
        for target,source in [('/usr/local/bin/sub','sub.sh'),('/usr/local/bin/geo-update','geo-update.sh'),
                              ('/usr/local/bin/traffic-update','traffic-update.py'),('/usr/local/bin/domain-usage','domain-usage.py')]:
            data=(ROOT/'scripts'/source).read_bytes()
            if source=='domain-usage.py':
                text=data.decode().replace('        conn = connect_db()\n        updated_at = load_meta(conn).get("last_poll_ok")\n        conn.close()\n','')
                text=text.replace('"updated_at": updated_at,','"updated_at": load_meta(conn).get("last_poll_ok"),')
                text=text.replace('    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")','        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")')
                data=text.encode()
            self.write(target,data)
        self.write('/var/lib/domain-usage/usage.db',b'fake historical database')
        self.write('/var/www/html/original.yaml',b'fake existing subscription')
        self.write('/root/.aws/credentials',b'fake credentials, forbidden to read')

    def write(self,path,data):
        file=self.root/path.lstrip('/'); file.parent.mkdir(parents=True,exist_ok=True)
        file.write_bytes(data)

    def run_command(self,args):
        self.calls.append(args)
        if args[:2]==['systemctl','is-active'] and args[-1] not in self.active: raise RuntimeError('inactive')
        if args[:2]==['systemctl','restart'] and self.fail_restart:
            self.fail_restart=False; raise RuntimeError('injected restart failure')
        return ''

    def test_check_is_read_only(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        changes=self.update.plan()
        self.assertEqual(len(changes),3)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.assertFalse(self.calls)
        self.assertFalse((self.root/'var/backups').exists())

    def test_update_and_rollback_never_read_private_runtime_files(self):
        protected=[self.root/p.lstrip('/') for p in mod.REQUIRED if not p.endswith('migrate.conf') and not p.startswith('/etc/nginx')]
        protected += [self.root/'var/lib/domain-usage/usage.db',self.root/'var/www/html/original.yaml',self.root/'root/.aws/credentials']
        before={p:p.read_bytes() for p in protected}
        old=(self.root/'usr/local/bin/traffic-update').read_bytes()
        original_read=Path.read_bytes
        def guarded(path):
            if path in protected: raise AssertionError('attempt to read private runtime file')
            return original_read(path)
        with patch.object(Path,'read_bytes',guarded), contextlib.redirect_stdout(io.StringIO()):
            stamp=self.update.apply()
            self.assertEqual(self.update.plan(),[])
            self.update.rollback(stamp)
        self.assertEqual(before,{p:p.read_bytes() for p in protected})
        self.assertEqual((self.root/'usr/local/bin/traffic-update').read_bytes(),old)
        self.assertNotIn(['systemctl','restart','hysteria-server.service'],self.calls)
        self.assertFalse((self.root/'usr/local/lib/lightsail-vpn/traffic-update.py').exists())

    def test_restart_failure_restores_code(self):
        before={path:self.update.path(path).read_bytes() for path in mod.FILES if self.update.path(path).exists()}
        self.fail_restart=True
        with self.assertRaises(RuntimeError),contextlib.redirect_stdout(io.StringIO()): self.update.apply()
        self.assertEqual(before,{path:self.update.path(path).read_bytes() for path in before})
        self.assertFalse((self.root/'usr/local/lib/lightsail-vpn/traffic-update.py').exists())

    def test_unknown_local_code_is_preserved(self):
        path=self.root/'usr/local/bin/sub'; path.write_bytes(b'custom local code')
        with self.assertRaises(ValueError): self.update.plan()
        self.assertEqual(path.read_bytes(),b'custom local code')

    def test_custom_units_preserved_by_default(self):
        path=self.root/'etc/systemd/system/geo-update.timer'; path.write_bytes(b'custom daily schedule')
        self.assertEqual(len(self.update.plan()),3)
        with self.assertRaises(ValueError): self.update.plan(units=True)
        with contextlib.redirect_stdout(io.StringIO()): self.update.apply()
        self.assertEqual(path.read_bytes(),b'custom daily schedule')
        with self.assertRaises(ValueError): self.update.plan(units=True)

    def test_incomplete_install_rejected(self):
        (self.root/'etc/hysteria/.sub-state').unlink()
        with self.assertRaises(ValueError): self.update.plan()

    def test_explicit_unit_update_and_rollback(self):
        repo=self.root/'new-repo'; repo.mkdir()
        for name in ('scripts','config','systemd'):
            shutil.copytree(ROOT/name,repo/name,ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(ROOT/'baseline.sha256',repo/'baseline.sha256')
        timer=repo/'systemd/geo-update.timer'
        timer.write_text(timer.read_text().replace('00,06,12,18:00:00','03:00:00'))
        self.update.repo=repo
        target=self.root/'etc/systemd/system/geo-update.timer'
        before=target.read_bytes()
        self.active.add('geo-update.timer')
        with contextlib.redirect_stdout(io.StringIO()): stamp=self.update.apply(units=True)
        self.assertEqual(target.read_bytes(),timer.read_bytes())
        self.assertIn(['systemctl','restart','geo-update.timer'],self.calls)
        self.assertNotIn(['systemctl','restart','hysteria-server.service'],self.calls)
        with contextlib.redirect_stdout(io.StringIO()): self.update.rollback(stamp)
        self.assertEqual(target.read_bytes(),before)

    def test_running_job_prevents_update(self):
        self.active.add('geo-update.service')
        with self.assertRaises(ValueError): self.update.apply()
        self.assertFalse((self.root/'var/backups/lightsail-vpn-update').exists())

    def test_rollback_rejects_new_local_changes(self):
        with contextlib.redirect_stdout(io.StringIO()): stamp=self.update.apply()
        (self.root/'usr/local/bin/domain-usage').write_bytes(b'new local change')
        with self.assertRaises(ValueError): self.update.rollback(stamp)

    def test_invalid_backup_id_rejected(self):
        with self.assertRaises(ValueError): self.update.rollback('../../root')
