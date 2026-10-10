"""真实 nginx -t；配置、证书、日志路径全在临时目录，不读取生产配置。"""
import importlib.util
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('render_sub',Path(__file__).parents[1]/'scripts/sub.py')
sub=importlib.util.module_from_spec(spec)
spec.loader.exec_module(sub)


@unittest.skipUnless(shutil.which('nginx') and shutil.which('openssl'),'requires nginx and openssl')
class TestNginx(unittest.TestCase):
    def test_generated_site_with_real_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cert,key=sub.new_certificate('8.8.8.8')
            (root/'cert').write_bytes(cert); (root/'key').write_bytes(key)
            with patch.object(sub,'cert_paths',return_value=(root/'cert',root/'key')):
                site=sub.nginx(dict(ip='8.8.8.8',https=True,name='vpn',token='dummy-test-token'))
            # nginx -t 也尝试 bind；用临时非特权端口，避免碰生产 80/443。
            for old in ('80','443 ssl'):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1',0))
                    port=sock.getsockname()[1]
                new=f'127.0.0.1:{port}'+(' ssl' if 'ssl' in old else '')
                site=site.replace(f'listen {old} default_server;',f'listen {new} default_server;')
            config=root/'nginx.conf'
            config.write_text(f'pid {root}/pid;\nerror_log stderr;\nevents {{}}\nhttp {{ access_log off;\n{site}\n}}')
            result=subprocess.run(['nginx','-t','-p',str(root),'-c',str(config)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
