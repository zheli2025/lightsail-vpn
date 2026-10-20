"""使用临时证书、假密码、随机本地端口验证初始化配置，不读取生产 YAML。"""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('server_bootstrap',ROOT/'scripts/bootstrap.py')
boot=importlib.util.module_from_spec(spec); spec.loader.exec_module(boot)
HYSTERIA=shutil.which('hysteria')


@unittest.skipUnless(HYSTERIA and shutil.which('openssl'),'requires hysteria/openssl')
class TestServerConfig(unittest.TestCase):
    def test_hysteria_accepts_generated_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); cert=root/'cert'; key=root/'key'
            subprocess.run(['openssl','req','-x509','-nodes','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1',
                            '-days','1','-subj','/CN=test','-out',str(cert),'-keyout',str(key)],check=True,capture_output=True)
            text=boot.configuration('dummy-auth','dummy-obfs','dummy-stats')
            text=text.replace('listen: :443','listen: 127.0.0.1:0').replace('127.0.0.1:25413','127.0.0.1:0')
            text=text.replace('/etc/hysteria/certs/server.crt',str(cert)).replace('/etc/hysteria/certs/server.key',str(key))
            config=root/'config.yaml'; config.write_text(text)
            child=subprocess.Popen([HYSTERIA,'server','--config',str(config)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            try:
                output,_=child.communicate(timeout=0.8)
                self.fail('Hysteria exited before test completed: '+output)
            except subprocess.TimeoutExpired:
                child.terminate(); output,_=child.communicate(timeout=5)
                self.assertIn('server up and running',output)
            finally:
                if child.poll() is None: child.kill(); child.communicate()
