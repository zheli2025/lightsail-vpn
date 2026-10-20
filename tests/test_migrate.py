import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

try: import yaml
except ImportError: yaml=None

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('migration',ROOT/'scripts/migrate.py')
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)


@unittest.skipUnless(yaml,'requires python3-yaml')
class TestMigration(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name); self.calls=[]; self.fail_at=None
        self.active={'hysteria-server.service','nginx','geo-update.timer'}
        self.enabled=set(self.active)
        self.m=mod.Migration(self.root,ROOT,self.run_command)
        self.args=mod.boot.parser().parse_args(['--ip','1.1.1.1','--node','测试节点','--name','new-vpn',
            '--region','ap-northeast-1','--instance-name','vpn-tokyo','--email','admin@example.com','--accept-tos'])
        self.args.upgrade_hysteria=False; self.args.replace_legacy_code=True
        self.args.archive_old_subscriptions=False
        self.cfg={'listen':':443','tls':{'cert':'/etc/hysteria/certs/server.crt','key':'/etc/hysteria/certs/server.key'},
                  'auth':{'type':'password','password':'fixture-original-auth'},
                  'obfs':{'type':'salamander','salamander':{'password':'fixture-original-obfs'}},
                  'resolver':{'type':'udp','udp':{'addr':'1.1.1.1:53'}},'disableUDP':False}
        self.write('/etc/hysteria/config.yaml',yaml.safe_dump(self.cfg))
        for path in ('/usr/local/bin/hysteria','/usr/local/bin/sub','/usr/local/bin/geo-update'):
            self.write(path,'old program fixture')
        self.write('/etc/hysteria/certs/server.crt','public fixture')
        self.write('/etc/hysteria/certs/server.key','private fixture; never overwritten')
        for unit in ('hysteria-server.service','geo-update.service','geo-update.timer'):
            self.write('/etc/systemd/system/'+unit,'old unit fixture')
        self.write('/etc/nginx/sites-available/default','old default fixture')
        enabled=self.root/'etc/nginx/sites-enabled'; enabled.mkdir()
        (enabled/'default').symlink_to(self.root/'etc/nginx/sites-available/default')
        self.write('/var/lib/domain-usage/usage.db','original historical fixture')
        self.write('/root/.aws/credentials','forbidden AWS fixture')
        self.enterContext(patch.object(mod.grp,'getgrnam',return_value=types.SimpleNamespace(gr_gid=0)))
        self.enterContext(patch('os.chown'))
        self.enterContext(patch.object(self.m,'probe_stats'))

    def write(self,path,data):
        file=self.root/path.lstrip('/'); file.parent.mkdir(parents=True,exist_ok=True)
        file.write_text(data)

    def run_command(self,args):
        self.calls.append(args)
        if self.fail_at and self.fail_at in ' '.join(args):
            self.fail_at=None; raise RuntimeError('injected failure')
        if args[:2]==['openssl','x509']: return 'IP Address:1.1.1.1\n'
        if args[0].endswith('/hysteria') and args[-1]=='version': return 'Version: v2.12.0'
        if args[:2]==['systemctl','is-active']:
            if args[-1] not in self.active: raise RuntimeError('inactive')
        if args[:2]==['systemctl','is-enabled']: return 'enabled' if args[-1] in self.enabled else 'disabled'
        if args[:2]==['systemctl','show']: return 'hysteria' if 'User' in args else 'not-found'
        if args[:2] in (['systemctl','stop'],['systemctl','disable']):
            # 迁移失败恢复时，尚未安装的单元返回错误，不能阻断文件恢复。
            if args[-1].startswith('domain-') and not (self.root/'etc/systemd/system'/args[-1]).exists():
                raise RuntimeError('not installed')
        if args[:2]==['systemctl','stop']: self.active.discard(args[-1])
        if args[:2]==['systemctl','disable']: self.enabled.discard(args[-1])
        if args[:2] in (['systemctl','start'],['systemctl','restart']): self.active.add(args[-1])
        if args[:2]==['systemctl','enable']:
            units=[u for u in args[2:] if not u.startswith('-')]
            self.enabled.update(units)
            if '--now' in args: self.active.update(units)
        if args[0].endswith('/sub'):
            self.write('/var/www/html/new-vpn.yaml','new private subscription fixture')
            self.write('/etc/nginx/sites-available/vpn-subscription','new site fixture')
            enabled=self.root/'etc/nginx/sites-enabled'
            (enabled/'default').unlink(missing_ok=True)
            if not (enabled/'vpn-subscription').is_symlink():
                (enabled/'vpn-subscription').symlink_to(self.root/'etc/nginx/sites-available/vpn-subscription')
            return 'subscription URL must never be printed'
        if args[0].endswith('/certbot'):
            self.write('/etc/letsencrypt/live/1.1.1.1/fullchain.pem','public certificate fixture')
            self.write('/etc/letsencrypt/live/1.1.1.1/privkey.pem','private fixture')
        return ''

    def test_check_changes_nothing_and_does_not_print_secrets(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        out=io.StringIO()
        with contextlib.redirect_stdout(out): self.m.report(self.args)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.assertNotIn('fixture-original-auth',out.getvalue())
        self.assertFalse(any(a[0] in ('apt-get','useradd','snap') for a in self.calls))

    def test_apply_keeps_proxy_credentials_certificates_extra_config_and_history(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out): self.m.apply(self.args)
        config=yaml.safe_load((self.root/'etc/hysteria/config.yaml').read_text())
        for key in self.cfg: self.assertEqual(config[key],self.cfg[key])
        self.assertEqual(config['trafficStats']['listen'],'127.0.0.1:25413')
        secret=(self.root/'etc/hysteria/.traffic-stats-secret').read_text().strip()
        self.assertEqual(config['trafficStats']['secret'],secret)
        self.assertEqual((self.root/'etc/hysteria/certs/server.key').read_text(),'private fixture; never overwritten')
        self.assertEqual((self.root/'var/lib/domain-usage/usage.db').read_text(),'original historical fixture')
        self.assertEqual((self.root/'root/.aws/credentials').read_text(),'forbidden AWS fixture')
        self.assertEqual((self.root/'usr/local/bin/hysteria').read_text(),'old program fixture')
        self.assertNotIn('fixture-original-auth',out.getvalue())
        self.assertNotIn('URL must never',out.getvalue())
        # 迁移后标准更新器能完整识别所有新组件。
        self.assertEqual(self.m.updater.plan(),[])

    def test_failure_restores_config_code_default_site_and_removes_new_state(self):
        before_config=(self.root/'etc/hysteria/config.yaml').read_bytes()
        before_sub=(self.root/'usr/local/bin/sub').read_bytes()
        self.fail_at='certonly'
        with self.assertRaises(RuntimeError),contextlib.redirect_stdout(io.StringIO()): self.m.apply(self.args)
        self.assertEqual((self.root/'etc/hysteria/config.yaml').read_bytes(),before_config)
        self.assertEqual((self.root/'usr/local/bin/sub').read_bytes(),before_sub)
        self.assertTrue((self.root/'etc/nginx/sites-enabled/default').is_symlink())
        self.assertFalse((self.root/'etc/nginx/sites-available/vpn-subscription').exists())
        self.assertFalse((self.root/'etc/hysteria/.sub-token').exists())

    def test_public_old_subscription_requires_explicit_archive(self):
        self.write('/var/www/html/old.yaml','old subscription fixture')
        with self.assertRaises(ValueError): self.m.apply(self.args)
        self.args.archive_old_subscriptions=True
        with contextlib.redirect_stdout(io.StringIO()): self.m.apply(self.args)
        self.assertFalse((self.root/'var/www/html/old.yaml').exists())
        copies=list((self.root/'var/backups/lightsail-vpn-migration').glob('*/file-*'))
        self.assertTrue(any(p.read_text()=='old subscription fixture' for p in copies))

    def test_unknown_code_requires_explicit_replacement(self):
        self.args.replace_legacy_code=False
        with self.assertRaises(ValueError): self.m.apply(self.args)
        self.assertFalse((self.root/'var/backups/lightsail-vpn-migration').exists())

    def test_other_sites_and_existing_state_rejected(self):
        self.write('/etc/nginx/sites-enabled/other','other site fixture')
        with self.assertRaises(ValueError): self.m.preflight(self.args)
        (self.root/'etc/nginx/sites-enabled/other').unlink()
        self.write('/etc/hysteria/.sub-token','existing fixture token')
        with self.assertRaises(ValueError): self.m.preflight(self.args)

    def test_wrong_ip_rejected_without_replacing_cert(self):
        self.args.ip='8.8.8.8'
        with self.assertRaises(ValueError): self.m.preflight(self.args)

    def test_extra_conf_d_server_is_not_silently_overridden(self):
        self.write('/etc/nginx/conf.d/other.conf','server {\n listen 80;\n}\n')
        with self.assertRaises(ValueError): self.m.preflight(self.args)

    def test_archive_is_restored_after_failure(self):
        self.write('/var/www/html/old.yaml','old private subscription fixture')
        self.args.archive_old_subscriptions=True
        self.fail_at='certonly'
        with self.assertRaises(RuntimeError),contextlib.redirect_stdout(io.StringIO()): self.m.apply(self.args)
        self.assertEqual((self.root/'var/www/html/old.yaml').read_text(),'old private subscription fixture')

    def test_malformed_config_error_does_not_reveal_secret(self):
        self.write('/etc/hysteria/config.yaml','auth: [secret-marker-sensitive: invalid: fixture')
        with self.assertRaises(ValueError) as caught: self.m.preflight(self.args)
        self.assertNotIn('secret-marker-sensitive',str(caught.exception))

    def test_explicit_binary_upgrade_is_backed_up_and_reverted_on_failure(self):
        self.args.upgrade_hysteria=True
        self.fail_at='certonly'
        with patch.object(self.m,'install_binary',side_effect=lambda:self.write('/usr/local/bin/hysteria','new verified binary fixture')):
            with self.assertRaises(RuntimeError),contextlib.redirect_stdout(io.StringIO()): self.m.apply(self.args)
        self.assertEqual((self.root/'usr/local/bin/hysteria').read_text(),'old program fixture')

    def test_stats_probe_failure_restores_original_proxy_config(self):
        old=(self.root/'etc/hysteria/config.yaml').read_bytes()
        self.m.probe_stats.side_effect=RuntimeError('injected API failure')
        with self.assertRaises(RuntimeError),contextlib.redirect_stdout(io.StringIO()): self.m.apply(self.args)
        self.assertEqual((self.root/'etc/hysteria/config.yaml').read_bytes(),old)
        self.assertFalse((self.root/'etc/hysteria/.traffic-stats-secret').exists())

    def test_empty_streams_api_is_valid(self):
        opener=types.SimpleNamespace(open=lambda *args,**kwargs:io.BytesIO(b'{"streams": null}'))
        with patch.object(mod.urllib.request,'build_opener',return_value=opener):
            mod.Migration.probe_stats(self.m,'fixture-secret')

    def test_migration_without_email_requests_no_email_registration(self):
        self.args.email=None
        with contextlib.redirect_stdout(io.StringIO()): self.m.apply(self.args)
        commands=[c for c in self.calls if c[0].endswith('/certbot')]
        self.assertEqual(len(commands),1)
        self.assertIn('--register-unsafely-without-email',commands[0])
        self.assertNotIn('--email',commands[0])

    def test_existing_managed_certificate_is_reused_without_account_changes(self):
        self.args.email=None
        self.write('/etc/letsencrypt/live/1.1.1.1/fullchain.pem','public certificate fixture')
        self.write('/etc/letsencrypt/live/1.1.1.1/privkey.pem','private fixture')
        self.write('/etc/letsencrypt/renewal/1.1.1.1.conf','renewal fixture')
        with contextlib.redirect_stdout(io.StringIO()): self.m.apply(self.args)
        self.assertFalse(any(c[0].endswith('/certbot') for c in self.calls))
        self.assertEqual((self.root/'etc/letsencrypt/live/1.1.1.1/privkey.pem').read_text(),'private fixture')
