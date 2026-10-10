"""只操作临时目录；不读取服务器状态、密钥或 Nginx 配置。"""
import argparse
import importlib.util
import json
import contextlib
import io
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import yaml

spec = importlib.util.spec_from_file_location('vpn_sub', Path(__file__).parents[1]/'scripts/sub.py')
sub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sub)


class TestSub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name in ('STATE','CONFIG','CERT','KEY','SUB','SITE','ENABLED','DEFAULT','LE','LOCK'):
            p = root/name
            self.enterContext(patch.object(sub,name,p))
        self.enterContext(patch.object(sub.grp,'getgrnam',return_value=types.SimpleNamespace(gr_gid=0)))
        self.enterContext(patch.object(sub.os,'chown'))
        self.enterContext(patch.object(sub,'new_certificate',return_value=(b'fake-public-cert',b'fake-private-key')))
        self.run = self.enterContext(patch.object(sub,'run',return_value=b''))
        self.state = dict(ip='8.8.8.8',name='vpn',node='东京 "测试"',email='admin@example.com',
                          auth='test-auth',obfs='test-obfs',token='test-token',https=True)
        self.make_le(self.state['ip'])

    def make_le(self,ip):
        cert,key = sub.cert_paths(ip)
        cert.parent.mkdir(parents=True,exist_ok=True)
        cert.write_text('fake'); key.write_text('fake')

    def test_cn_routing_and_yaml(self):
        data=yaml.safe_load(yaml.safe_dump(sub.subscription(self.state),allow_unicode=True))
        self.assertEqual(data['proxies'][0]['name'],self.state['node'])
        self.assertEqual(data['rules'][-3:],['RULE-SET,cn-domain,DIRECT','RULE-SET,cn-ip,DIRECT','MATCH,PROXY'])
        self.assertNotIn('geox-url',data)
        self.assertEqual(data['rule-providers']['cn-ip']['interval'],86400)

    def test_no_public_subscription_bypass(self):
        site=sub.nginx(self.state)
        self.assertIn('if ($arg_token != "test-token")',site)
        self.assertNotIn('try_files',site)
        self.assertIn('location / { return 404; }',site)
        http=sub.nginx(dict(self.state,https=False))
        self.assertNotIn('subscription.yaml',http)
        self.assertNotIn('test-token',http)

    def test_token_rotation_and_permissions(self):
        sub.publish(self.state)
        new=dict(self.state,token='fresh-token')
        sub.publish(new)
        self.assertEqual(sub.load()['token'],'fresh-token')
        self.assertNotIn('test-token',sub.SITE.read_text())
        self.assertEqual(sub.STATE.stat().st_mode & 0o777,0o600)
        self.assertEqual(sub.SUB.stat().st_mode & 0o777,0o640)

    def test_ip_switch_keeps_token(self):
        sub.publish(self.state)
        self.make_le('1.1.1.1')
        new=dict(self.state,ip='1.1.1.1')
        sub.publish(new,restart=True,regenerate=True)
        self.assertEqual(sub.load()['token'],self.state['token'])
        data=yaml.safe_load(sub.SUB.read_text())
        self.assertEqual(data['proxies'][0]['server'],'1.1.1.1')
        self.assertIn('https://1.1.1.1/',data['rule-providers']['cn-domain']['url'])

    def test_nginx_failure_rolls_back(self):
        sub.publish(self.state)
        paths=[sub.STATE,sub.CONFIG,sub.SUB,sub.SITE,sub.CERT,sub.KEY]
        old={p:p.read_bytes() for p in paths}
        def fail(*args):
            if args == ('nginx','-t'): raise RuntimeError('injected config failure')
            return b''
        self.run.side_effect=fail
        with self.assertRaises(RuntimeError): sub.publish(dict(self.state,token='new-token'))
        self.assertEqual(old,{p:p.read_bytes() for p in paths})
        self.assertTrue(sub.ENABLED.is_symlink())

    def test_restart_failure_rolls_back(self):
        sub.publish(self.state)
        old=sub.CONFIG.read_bytes()
        calls=0
        def fail(*args):
            nonlocal calls
            if args == ('systemctl','restart','hysteria-server.service'):
                calls+=1
                if calls==1: raise RuntimeError('injected restart failure')
            return b''
        self.run.side_effect=fail
        with self.assertRaises(RuntimeError): sub.publish(dict(self.state,auth='new-auth'),restart=True)
        self.assertEqual(sub.CONFIG.read_bytes(),old)
        self.assertEqual(sub.load(),self.state)
        self.assertEqual(calls,2)

    def test_missing_cert_does_not_change_files(self):
        sub.publish(self.state)
        old=sub.STATE.read_bytes()
        with self.assertRaises(RuntimeError): sub.publish(dict(self.state,ip='1.1.1.1'))
        self.assertEqual(sub.STATE.read_bytes(),old)

    def test_reject_bad_parameters(self):
        for value in ['127.0.0.1','10.0.0.1','999.1.1.1','1.2.3.4.','224.0.0.1']:
            with self.assertRaises(argparse.ArgumentTypeError): sub.ipv4(value)
        for value in ['../../etc/passwd','bad name','']:
            with self.assertRaises(argparse.ArgumentTypeError): sub.valid_name(value)
        with self.assertRaises(argparse.ArgumentTypeError): sub.valid_node('PROXY')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sub.parser().parse_args(['token','extra'])

    def test_actual_token_command_generates_new_token(self):
        sub.publish(self.state)
        with patch.object(sub.sys,'argv',['sub','token']), patch.object(sub.os,'geteuid',return_value=0):
            with contextlib.redirect_stdout(io.StringIO()): sub.main()
        self.assertNotEqual(sub.load()['token'],self.state['token'])
        self.assertRegex(sub.load()['token'],r'^[a-f0-9]{64}$')


if __name__=='__main__': unittest.main()
