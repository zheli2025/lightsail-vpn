"""原版 Bash sub 的隔离测试：模拟服务和证书操作，不读取生产配置。"""
import os
from pathlib import Path
import subprocess
import shutil
import socket
import tempfile
import textwrap
import unittest

ROOT=Path(__file__).resolve().parents[1]


class TestSub(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name); self.bin=self.root/'bin'; self.bin.mkdir()
        self.web=self.root/'www'; self.web.mkdir(); (self.web/'geo').mkdir()
        self.nginx=self.root/'nginx'; self.nginx.mkdir()
        (self.nginx/'default').write_text('default')
        (self.nginx/'default-enabled').symlink_to(self.nginx/'default')
        for name in ('geosite-cn.mrs','geoip-cn.mrs'): (self.web/'geo'/name).write_text('fixture')
        self.cert=self.root/'server.crt'; self.cert.write_text('IP Address:1.1.1.1')
        (self.root/'server.key').write_text('dummy-private-key')
        (self.root/'config.yaml').write_text('listen: :443\nauth:\n  type: password\n  password: "fixture-auth"\nobfs:\n  type: salamander\n  salamander:\n    password: "fixture-obfs"\n')
        (self.root/'token').write_text('a'*64+'\n')
        le=self.root/'le/live/1.1.1.1'; le.mkdir(parents=True)
        (le/'fullchain.pem').write_text('fake-public-cert'); (le/'privkey.pem').write_text('dummy-key')
        self.stub('nginx','''#!/bin/sh
exit "${FAIL_NGINX:-0}"
''')
        self.stub('systemctl','''#!/bin/sh
echo "$*" >> "$CMD_LOG"
if [ "$1" = is-active ]; then echo active; fi
exit 0
''')
        self.stub('chown','#!/bin/sh\nexit 0\n')
        self.stub('openssl','''#!/usr/bin/python3
import os,sys
from pathlib import Path
a=sys.argv[1:]
if a[0]=='rand': os.execv('/usr/bin/openssl',['openssl']+a)
elif a[0]=='x509': print(Path(a[a.index('-in')+1]).read_text())
elif a[0]=='req':
    Path(a[a.index('-keyout')+1]).write_text('dummy-new-key')
    Path(a[a.index('-out')+1]).write_text('IP Address:'+a[a.index('-subj')+1].split('=')[1])
''')
        self.stub('install','''#!/usr/bin/python3
import os,shutil,sys
a=sys.argv[1:]; mode=0o755; paths=[]; directory=False
while a:
    v=a.pop(0)
    if v in ('-o','-g'): a.pop(0)
    elif v=='-m': mode=int(a.pop(0),8)
    elif v=='-d': directory=True
    else: paths.append(v)
if directory:
    for p in paths: os.makedirs(p,mode=mode,exist_ok=True)
else: shutil.copyfile(paths[0],paths[1]); os.chmod(paths[1],mode)
''')
        self.stub('certbot','''#!/usr/bin/python3
import os,sys
from pathlib import Path
a=sys.argv[1:]; base=Path(os.environ['LETSENCRYPT_DIR'])/'live'
if a[0]=='certonly':
    p=base/a[a.index('--ip-address')+1]; p.mkdir(parents=True,exist_ok=True)
    (p/'fullchain.pem').write_text('fake-public'); (p/'privkey.pem').write_text('dummy-key')
elif a[0]=='delete':
    p=base/a[a.index('--cert-name')+1]
    for file in p.iterdir(): file.unlink()
    p.rmdir()
''')
        # 仅在测试副本中跳过 root 检查，所有其它代码保持原样。
        script=(ROOT/'scripts/sub.sh').read_text().replace('[[ $EUID -eq 0 ]] || die','true || die',1)
        self.script=self.root/'sub.sh'; self.script.write_text(script)
        self.env={'PATH':str(self.bin)+':/usr/bin:/bin','LANG':'C.UTF-8','CMD_LOG':str(self.root/'calls'),
                  'HYSTERIA_CONFIG':str(self.root/'config.yaml'),'CERT_FILE':str(self.cert),
                  'WEBROOT':str(self.web),'STATE_FILE':str(self.root/'state'),'NODE_FILE':str(self.root/'node'),
                  'TOKEN_FILE':str(self.root/'token'),'NGINX_SITE':str(self.nginx/'site'),
                  'NGINX_ENABLED':str(self.nginx/'enabled'),'NGINX_DEFAULT':str(self.nginx/'default'),
                  'NGINX_DEFAULT_ENABLED':str(self.nginx/'default-enabled'),'BACKUP_DIR':str(self.nginx/'backup'),
                  'DEFAULT_NAME':'test-vpn','DEFAULT_NODE':'Test-Node',
                  'TRAFFIC_NGINX_SNIPPET':str(self.nginx/'userinfo'),'TRAFFIC_STATE_FILE':str(self.root/'traffic-state'),
                  'TRAFFIC_SERVICE_OVERRIDE':str(self.root/'traffic-override'),
                  'LETSENCRYPT_DIR':str(self.root/'le'),'CERTBOT_BIN':str(self.bin/'certbot')}

    def stub(self,name,content):
        file=self.bin/name; file.write_text(content); file.chmod(0o755)

    def command(self,*args,success=True):
        result=subprocess.run(['bash',str(self.script),*args],env=self.env,capture_output=True,text=True)
        if success: self.assertEqual(result.returncode,0,result.stderr)
        else: self.assertNotEqual(result.returncode,0)
        return result

    def test_update_original_rules_and_http_https(self):
        self.command('update')
        yaml=(self.web/'test-vpn.yaml').read_text(); site=(self.nginx/'site').read_text()
        self.assertIn('RULE-SET,cn-ip,DIRECT,no-resolve',yaml)
        self.assertIn('interval: 86400',yaml)
        self.assertIn('password: "fixture-auth"',yaml)
        self.assertIn('listen 80;',site); self.assertIn('listen 443 ssl;',site)
        self.assertIn('include '+str(self.nginx/'userinfo'),site)

    def test_token_rotates_and_validation_rolls_back(self):
        self.command('update'); old=(self.root/'token').read_text()
        self.command('token'); new=(self.root/'token').read_text()
        self.assertNotEqual(old,new)
        before=(self.nginx/'site').read_text()
        self.env['FAIL_NGINX']='1'
        self.command('token',success=False)
        self.assertEqual((self.root/'token').read_text(),new)
        self.assertEqual((self.nginx/'site').read_text(),before)

    def test_name_node_and_rotate(self):
        self.command('update'); self.command('name','other-vpn'); self.command('node','新节点')
        self.command('rotate')
        self.assertFalse((self.web/'test-vpn.yaml').exists())
        text=(self.web/'other-vpn.yaml').read_text()
        self.assertIn('新节点',text); self.assertNotIn('fixture-auth',text)

    def test_ip_updates_statistics_and_keeps_token(self):
        self.command('update')
        override=self.root/'traffic-override'; override.write_text('[Service]\nEnvironment=LS_PUBLIC_IP=1.1.1.1\n')
        token=(self.root/'token').read_text()
        self.command('ip','8.8.8.8')
        self.assertIn('server: 8.8.8.8',(self.web/'test-vpn.yaml').read_text())
        self.assertIn('LS_PUBLIC_IP=8.8.8.8',override.read_text())
        self.assertEqual((self.root/'token').read_text(),token)
        self.assertFalse((self.root/'le/live/1.1.1.1').exists())

    def test_usage_and_traffic_dispatch(self):
        self.stub('usage-mock','#!/bin/sh\nprintf "%s\\n" "$*"\n')
        self.env['DOMAIN_USAGE_BIN']=str(self.bin/'usage-mock')
        self.env['TRAFFIC_UPDATE_BIN']=str(self.bin/'usage-mock')
        self.assertIn('report month --json',self.command('usage','month','--json').stdout)
        self.assertIn('tui',self.command('usage','tui').stdout)
        self.assertIn('update',self.command('traffic','update').stdout)

    @unittest.skipUnless(shutil.which('nginx') and shutil.which('openssl'),'requires nginx/openssl')
    def test_real_nginx_syntax_in_isolated_config(self):
        pub=self.root/'public.crt'; key=self.root/'private.key'
        subprocess.run(['/usr/bin/openssl','req','-x509','-nodes','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1',
                        '-days','1','-subj','/CN=test','-keyout',str(key),'-out',str(pub)],check=True,capture_output=True)
        self.env.update(TLS_CERT_FILE=str(pub),TLS_KEY_FILE=str(key))
        self.command('update')
        site=(self.nginx/'site').read_text()
        site='\n'.join(line for line in site.splitlines() if 'listen [::]' not in line)
        for old in ('80','443 ssl'):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            new=f'127.0.0.1:{port}'+(' ssl' if 'ssl' in old else '')
            site=site.replace(f'listen {old};',f'listen {new};')
        file=self.root/'isolated-nginx.conf'
        file.write_text(f'pid {self.root}/pid;\nerror_log stderr;\nevents {{}}\nhttp {{ access_log off;\n{site}\n}}')
        result=subprocess.run(['nginx','-t','-p',str(self.root),'-c',str(file)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
