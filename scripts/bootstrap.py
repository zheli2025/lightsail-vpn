#!/usr/bin/env python3
"""仅用于新实例初始化。参数文件不含密码；运行密码只在目标机 /etc/hysteria 生成。"""
import argparse
import grp
import ipaddress
import json
import os
import pwd
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

MARKER=Path('/etc/lightsail-vpn/install.json')
BASE=Path('/etc/hysteria')
TRAFFIC=Path('/etc/systemd/system/traffic-update.service.d/migrate.conf')


def ipv4(value):
    try:
        ip=ipaddress.IPv4Address(value)
        if not ip.is_global or ip.is_multicast: raise ValueError()
        return str(ip)
    except ValueError: raise argparse.ArgumentTypeError('必须是公网 IPv4') from None


def parser():
    p=argparse.ArgumentParser(description='复现原服务：sudo bash deploy.sh --ip IP --node 节点名 --name 订阅名 --region AWS地区 --instance-name 实例名 [--email 邮箱] --accept-tos')
    p.add_argument('--ip',type=ipv4,required=True)
    p.add_argument('--node',required=True)
    p.add_argument('--name',default='aws-hysteria')
    p.add_argument('--region',required=True)
    p.add_argument('--instance-name',required=True)
    p.add_argument('--email',default=None,help='可选 ACME 联系邮箱；省略时不登记邮箱，已有账户保持不变')
    p.add_argument('--accept-tos',action='store_true',help='接受 https://letsencrypt.org/repository/ 中的服务条款')
    p.add_argument('--validate-only',action='store_true',help=argparse.SUPPRESS)
    p.add_argument('--tls',action='store_true',help=argparse.SUPPRESS)
    return p


def validate(a):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}',a.name):
        raise ValueError('订阅名限 1–32 位小写字母、数字、连字符')
    if not 1<=len(a.node)<=32 or any(ord(c)<32 or c in '\\"' for c in a.node):
        raise ValueError('节点名限 1–32 字符，不含双引号、反斜杠和控制字符')
    if a.node in ('DIRECT','REJECT','PROXY'): raise ValueError('节点名不能与策略名同名')
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d',a.region): raise ValueError('AWS 地区格式无效')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}',a.instance_name): raise ValueError('Lightsail 实例名格式无效')
    if a.email is not None and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',a.email): raise ValueError('邮箱格式无效')
    if not a.accept_tos: raise ValueError('请阅读服务条款并加入 --accept-tos')


def params(a):
    return {k:getattr(a,k) for k in ('ip','node','name','region','instance_name','email')}


def certbot_command(a,webroot='/var/www/html',binary='/snap/bin/certbot'):
    contact=['--email',a.email] if a.email is not None else ['--register-unsafely-without-email']
    return [str(binary),'certonly','--non-interactive','--agree-tos',*contact,
            '--preferred-profile','shortlived','--webroot','--webroot-path',str(webroot),
            '--ip-address',a.ip,'--keep-until-expiring']


def reusable_certificate(ip,live_dir=Path('/etc/letsencrypt/live'),run=None):
    cert=Path(live_dir)/ip/'fullchain.pem'; key=Path(live_dir)/ip/'privkey.pem'
    renewal=Path(live_dir).parent/'renewal'/(ip+'.conf')
    if not cert.is_file() or not key.is_file() or not renewal.is_file(): return False
    invoke=run or (lambda command:subprocess.run(command,check=True,capture_output=True))
    try:
        # 只检查公开证书的 IP 和有效期；不读取私钥内容或账户文件。
        invoke(['openssl','x509','-in',str(cert),'-noout','-checkip',ip])
        invoke(['openssl','x509','-in',str(cert),'-noout','-checkend','86400'])
    except (OSError,RuntimeError,subprocess.CalledProcessError): return False
    return True


def atomic(path,text,mode=0o600,group=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.init-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.chmod(tmp,mode)
        if group: os.chown(tmp,0,grp.getgrnam(group).gr_gid)
        os.replace(tmp,path)
    finally: Path(tmp).unlink(missing_ok=True)


def configuration(auth,obfs,stats):
    # 原版 sub 依赖密码双引号格式，不能用 YAML 序列化器的无引号标量代替。
    return f'''listen: :443
tls:
  cert: /etc/hysteria/certs/server.crt
  key: /etc/hysteria/certs/server.key
auth:
  type: password
  password: "{auth}"
obfs:
  type: salamander
  salamander:
    password: "{obfs}"
trafficStats:
  listen: 127.0.0.1:25413
  secret: "{stats}"
'''


def initialize(a):
    wanted=params(a)
    existed=MARKER.exists()
    if existed:
        previous=json.loads(MARKER.read_text())
        # 联系邮箱不决定服务归属；旧安装可省略 --email 重跑，保留既有登记信息。
        if any(previous.get(k)!=v for k,v in wanted.items() if k!='email'):
            raise ValueError('部署参数与首次安装不同；用 sub 管理已有安装，不要用改参数重跑迁移')
    if not existed:
        if (BASE/'config.yaml').exists(): raise ValueError('发现既有配置，拒绝接管')
        atomic(MARKER,json.dumps(wanted,ensure_ascii=False,indent=2)+'\n')
    secret=BASE/'.traffic-stats-secret'
    config=BASE/'config.yaml'
    if not config.exists():
        stats=secrets.token_hex(32)
        atomic(secret,stats+'\n',0o640,'usage-tracker')
        atomic(config,configuration(secrets.token_hex(16),secrets.token_hex(16),stats),0o644)
    elif not secret.exists():
        raise ValueError('已有配置但统计 API secret 缺失，停止以免生成不匹配的 secret')
    cert=BASE/'certs/server.crt'; key=BASE/'certs/server.key'
    if not cert.exists() or not key.exists():
        cert.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='vpn-new-cert-') as tmp:
            subprocess.run(['openssl','req','-x509','-nodes','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1',
                            '-days','3650','-subj',f'/CN={a.ip}','-addext',f'subjectAltName=IP:{a.ip}',
                            '-out',tmp+'/cert','-keyout',tmp+'/key'],check=True,capture_output=True)
            for dest,src,mode in [(cert,'cert',0o644),(key,'key',0o600)]:
                atomic(dest,Path(tmp,src).read_text(),mode,'hysteria')
                os.chown(dest,pwd.getpwnam('hysteria').pw_uid,grp.getgrnam('hysteria').gr_gid)
    for file,value in [('.sub-state',f'/var/www/html/{a.name}.yaml'),('.sub-node',a.node),('.sub-token',None)]:
        path=BASE/file
        if not path.exists(): atomic(path,(value if value is not None else secrets.token_hex(32))+'\n')
    if not TRAFFIC.exists():
        atomic(TRAFFIC,f'[Service]\nEnvironment=LS_REGION={a.region}\nEnvironment=LS_INSTANCE_NAME={a.instance_name}\nEnvironment=LS_PRIVATE_IP=\nEnvironment=LS_PUBLIC_IP={a.ip}\n',0o644)
    print('初始化完成；已有密码、令牌、证书及统计文件均保留')


def main():
    a=parser().parse_args(); validate(a)
    if a.validate_only: return
    if os.geteuid()!=0: raise ValueError('必须 sudo 运行')
    if a.tls:
        if reusable_certificate(a.ip):
            print('复用现有有效 IP HTTPS 证书，不修改账户邮箱')
            return
        subprocess.run(certbot_command(a),check=True)
        return
    initialize(a)


if __name__=='__main__':
    try: main()
    except ValueError as exc:
        sys.exit(f'error: {exc}')
    except (OSError,subprocess.CalledProcessError) as exc:
        sys.exit(f'error: {type(exc).__name__}；检查参数和操作权限，勿输出配置内容排错')
