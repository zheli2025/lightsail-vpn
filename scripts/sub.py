#!/usr/bin/env python3
"""订阅管理。只在目标部署服务器运行；状态/密码不存放在 git 仓库。"""
import argparse
import copy
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import grp
import re
import secrets
import subprocess
import sys
import tempfile
import yaml

STATE = Path('/etc/lightsail-vpn/state.json')
CONFIG = Path('/etc/hysteria/config.yaml')
CERT = Path('/etc/hysteria/certs/server.crt')
KEY = Path('/etc/hysteria/certs/server.key')
SUB = Path('/var/lib/lightsail-vpn/subscription.yaml')
SITE = Path('/etc/nginx/sites-available/lightsail-vpn')
ENABLED = Path('/etc/nginx/sites-enabled/lightsail-vpn')
DEFAULT = Path('/etc/nginx/sites-enabled/default')
WEBROOT = '/var/www/lightsail-vpn'
DATA = '/var/lib/lightsail-vpn'
LE = Path('/etc/letsencrypt/live')
LOCK = Path('/run/lightsail-vpn-sub.lock')


def run(*args):
    # 不在异常中回显命令行/输出，避免配置或凭据被错误处理流程输出。
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode:
        raise RuntimeError(f'{Path(args[0]).name} 执行失败；检查对应服务日志')
    return r.stdout


def ipv4(value):
    try:
        a = ipaddress.IPv4Address(value)
        if not a.is_global or a.is_multicast:
            raise ValueError()
    except ValueError:
        raise argparse.ArgumentTypeError('需要有效的公网 IPv4') from None
    return str(a)


def valid_name(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', value):
        raise argparse.ArgumentTypeError('订阅名需为 1–32 位小写字母、数字、连字符')
    return value


def valid_node(value):
    if not 1 <= len(value) <= 64 or any(ord(c) < 32 for c in value):
        raise argparse.ArgumentTypeError('节点名需为 1–64 字符，不含控制字符')
    if value in ('DIRECT','REJECT','REJECT-DROP','PASS','COMPATIBLE','PROXY'):
        raise argparse.ArgumentTypeError('节点名不能与 Clash 内置策略或 PROXY 组同名')
    return value


def atomic(path, contents, mode=0o600, group=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.vpn-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(contents.encode() if isinstance(contents, str) else contents)
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        if group:
            os.chown(tmp, 0, grp.getgrnam(group).gr_gid)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def load():
    if not STATE.exists():
        raise RuntimeError('尚未部署，请先运行 deploy.sh')
    return json.loads(STATE.read_text())


def cert_paths(ip):
    return LE / ip / 'fullchain.pem', LE / ip / 'privkey.pem'


def subscription(s):
    node = s['node']
    providers = {}
    for key, kind, file in [('cn-domain', 'domain', 'geosite-cn.mrs'), ('cn-ip', 'ipcidr', 'geoip-cn.mrs')]:
        providers[key] = dict(type='http', behavior=kind, format='mrs',
                              url=f'https://{s["ip"]}/geo/{file}',
                              path=f'./rule-set/{file}', interval=86400)
    rules = [f'IP-CIDR,{cidr},DIRECT,no-resolve' for cidr in
             ['127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10']]
    rules += [f'IP-CIDR6,{cidr},DIRECT,no-resolve' for cidr in ['::1/128', 'fc00::/7', 'fe80::/10']]
    # CN IP 兜底允许 DNS 解析，避免漏掉未命中域名集的大陆目的地。
    rules += ['RULE-SET,cn-domain,DIRECT', 'RULE-SET,cn-ip,DIRECT', 'MATCH,PROXY']
    return {'mixed-port': 7890, 'allow-lan': False, 'mode': 'rule', 'log-level': 'info', 'ipv6': False,
            'proxies': [dict(name=node, type='hysteria2', server=s['ip'], port=443,
                             password=s['auth'], obfs='salamander',
                             **{'obfs-password': s['obfs'], 'skip-cert-verify': True})],
            'rule-providers': providers,
            'proxy-groups': [dict(name='PROXY', type='select', proxies=[node, 'DIRECT'])], 'rules': rules}


def server(s):
    return {'listen': ':443', 'tls': {'cert': str(CERT), 'key': str(KEY)},
            'auth': {'type': 'password', 'password': s['auth']},
            'obfs': {'type': 'salamander', 'salamander': {'password': s['obfs']}}}


def nginx(s):
    # HTTP 仅用于 ACME；不提供明文订阅，也不把凭据文件放入公开 webroot。
    result = f'''server {{
    listen 80 default_server;
    server_name _;
    location ^~ /.well-known/acme-challenge/ {{ root {WEBROOT}; access_log off; }}
    location / {{ return 404; }}
}}
'''
    if not s['https']:
        return result
    cert, key = cert_paths(s['ip'])
    return result + f'''server {{
    listen 443 ssl default_server;
    server_name _;
    ssl_certificate {cert};
    ssl_certificate_key {key};
    ssl_protocols TLSv1.2 TLSv1.3;
    location = /{s['name']}.yaml {{
        if ($arg_token != "{s['token']}") {{ return 403; }}
        alias {SUB};
        default_type text/plain;
        add_header Cache-Control "no-store";
        access_log off;
    }}
    location = /geo/geosite-cn.mrs {{ alias {DATA}/geo/geosite-cn.mrs; access_log off; }}
    location = /geo/geoip-cn.mrs {{ alias {DATA}/geo/geoip-cn.mrs; access_log off; }}
    location / {{ return 404; }}
}}
'''


def new_certificate(ip):
    with tempfile.TemporaryDirectory(prefix='vpn-cert-') as tmp:
        cert, key = Path(tmp)/'cert', Path(tmp)/'key'
        run('openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
            '-nodes', '-days', '3650', '-subj', f'/CN={ip}', '-addext', f'subjectAltName=IP:{ip}',
            '-keyout', str(key), '-out', str(cert))
        return cert.read_bytes(), key.read_bytes()


def snapshot(path):
    if not path.exists():
        return None
    st = path.stat()
    return path.read_bytes(), st.st_mode & 0o777, st.st_uid, st.st_gid


def restore(path, item):
    if item is None:
        path.unlink(missing_ok=True)
    else:
        data, mode, uid, gid = item
        atomic(path, data, mode)
        os.chown(path, uid, gid)


def publish(s, restart=False, regenerate=False):
    """锁内发布；验证或重启失败则恢复文件和旧配置。进程强杀/断电不保证事务性。"""
    if s['https'] and not all(p.exists() for p in cert_paths(s['ip'])):
        raise RuntimeError('HTTPS 证书缺失；运行 sudo sub tls')
    paths = [STATE, CONFIG, SUB, SITE, CERT, KEY]
    before = {p: snapshot(p) for p in paths}
    links = {}
    for p in (ENABLED, DEFAULT):
        if p.exists() and not p.is_symlink():
            raise RuntimeError(f'{p} 不是符号链接，拒绝覆盖')
        links[p] = os.readlink(p) if p.is_symlink() else None
    try:
        if regenerate or not CERT.exists() or not KEY.exists():
            cert, key = new_certificate(s['ip'])
            atomic(CERT, cert, 0o640, 'hysteria')
            atomic(KEY, key, 0o640, 'hysteria')
        atomic(CONFIG, yaml.safe_dump(server(s), allow_unicode=True), 0o640, 'hysteria')
        atomic(SUB, yaml.safe_dump(subscription(s), allow_unicode=True, sort_keys=False), 0o640, 'www-data')
        atomic(SITE, nginx(s), 0o600)
        DEFAULT.unlink(missing_ok=True)
        ENABLED.unlink(missing_ok=True)
        ENABLED.symlink_to(SITE)
        run('nginx', '-t')
        run('systemctl', 'reload-or-restart', 'nginx')
        if restart:
            run('systemctl', 'restart', 'hysteria-server.service')
            run('systemctl', 'is-active', '--quiet', 'hysteria-server.service')
        atomic(STATE, json.dumps(s, ensure_ascii=False, indent=2)+'\n')
    except Exception:
        for p, item in before.items():
            restore(p, item)
        for p, target in links.items():
            p.unlink(missing_ok=True)
            if target is not None:
                p.symlink_to(target)
        try:
            run('systemctl', 'reload-or-restart', 'nginx')
            if restart and before[CONFIG] is not None:
                run('systemctl', 'restart', 'hysteria-server.service')
        except RuntimeError:
            print('恢复后的服务启动失败，请检查 systemctl status', file=sys.stderr)
        raise


def obtain(ip, email):
    run('/snap/bin/certbot', 'certonly', '--non-interactive', '--agree-tos', '--email', email,
        '--required-profile', 'shortlived', '--webroot', '--webroot-path', WEBROOT,
        '--cert-name', ip, '--ip-address', ip, '--keep-until-expiring')
    if not all(p.exists() for p in cert_paths(ip)):
        raise RuntimeError('Certbot 未生成预期的 IP 证书')


def parser():
    p = argparse.ArgumentParser(description='Lightsail VPN 订阅管理（密码仅保存在服务器）')
    commands = p.add_subparsers(dest='command', required=True)
    for command in ('update', 'url', 'status', 'token', 'rotate', 'tls'):
        commands.add_parser(command)
    for command, validator in [('name', valid_name), ('node', valid_node)]:
        commands.add_parser(command).add_argument('value', nargs='?', type=validator)
    commands.add_parser('ip').add_argument('value', type=ipv4)
    commands.add_parser('geo').add_argument('action', nargs='?', choices=['update','status'], default='status')
    init = commands.add_parser('init', help='仅用于 deploy.sh 首次安装/恢复')
    init.add_argument('--ip', required=True, type=ipv4)
    init.add_argument('--name', required=True, type=valid_name)
    init.add_argument('--node', required=True, type=valid_node)
    init.add_argument('--email', required=True)
    return p


def main():
    args = parser().parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('请用 sudo sub 运行')
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.command == 'init':
            if STATE.exists():
                s = load()
                if any(s[k] != getattr(args,k) for k in ('ip','node','name','email')):
                    raise RuntimeError('已有部署参数不同；更换 IP/名称请用 sub 管理命令')
                print('保留现有密码与令牌'); return
            s = {k: getattr(args,k) for k in ('ip','node','name','email')}
            s.update(auth=secrets.token_hex(32), obfs=secrets.token_hex(32),
                     token=secrets.token_hex(32), https=False)
            publish(s)
            print('初始化完成，尚未开放订阅；下一步申请 HTTPS 证书'); return
        if args.command == 'geo':
            print(run('/usr/local/bin/geo-update', args.action).decode(), end=''); return
        s = load()
        if args.command == 'url':
            if not s['https']:
                raise RuntimeError('HTTPS 尚未就绪，请运行 sudo sub tls')
            print(f'https://{s["ip"]}/{s["name"]}.yaml?token={s["token"]}'); return
        if args.command == 'status':
            print(f'IP: {s["ip"]}\n节点: {s["node"]}\n订阅名: {s["name"]}\nHTTPS 已配置: {s["https"]}')
            for unit in ['nginx','hysteria-server.service','geo-update.timer','snap.certbot.renew.timer']:
                r = subprocess.run(['systemctl','is-active',unit], capture_output=True, text=True)
                print(f'{unit}: {r.stdout.strip()}')
            cert, _ = cert_paths(s['ip'])
            if cert.exists():
                print(run('openssl','x509','-in',str(cert),'-noout','-enddate').decode().strip())
            return
        new = copy.deepcopy(s)
        restart = regenerate = False
        if args.command in ('name','node'):
            if args.value is None:
                print(s[args.command]); return
            new[args.command] = args.value
        elif args.command == 'token':
            new['token'] = secrets.token_hex(32)
        elif args.command == 'rotate':
            new.update(auth=secrets.token_hex(32), obfs=secrets.token_hex(32)); restart=True
        elif args.command in ('ip','tls'):
            if args.command == 'ip':
                new['ip'] = args.value
                restart = regenerate = new['ip'] != s['ip']
            print('申请/确认 IP HTTPS 证书；需 IP 已绑定且公网 TCP 80 可达')
            obtain(new['ip'],new['email'])
            new['https'] = True
        if args.command != 'tls' and not new['https']:
            raise RuntimeError('请先执行 sudo sub tls 完成 HTTPS 初始化')
        publish(new,restart=restart,regenerate=regenerate)
        print('配置已更新。查看订阅 URL：sudo sub url')
        if args.command == 'ip' and new['ip'] != s['ip']:
            print(f'旧证书保留用于回退；确认稳定后可手动删除旧证书 {s["ip"]}（见 README）')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError,OSError,ValueError,KeyError) as exc:
        sys.exit(f'error: {exc}')
