#!/usr/bin/env python3
"""显式迁移基础旧安装。秘密只在目标机内处理，不输出或上传。"""
import argparse
import datetime
import fcntl
import grp
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO=Path(__file__).resolve().parents[1]


def load(name,file):
    spec=importlib.util.spec_from_file_location(name,REPO/'scripts'/file)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


boot=load('migration_bootstrap','bootstrap.py')
up=load('migration_update','update.py')
layout=load('migration_nginx_layout','nginx_layout.py')
SNAPSHOT=[*up.FILES,'/usr/local/bin/hysteria','/etc/hysteria/config.yaml',
          '/etc/hysteria/.sub-state','/etc/hysteria/.sub-node','/etc/hysteria/.sub-token',
          '/etc/hysteria/.traffic-stats-secret','/etc/nginx/sites-available/vpn-subscription',
          '/etc/nginx/sites-enabled/vpn-subscription','/etc/nginx/sites-enabled/default',
          '/etc/nginx/subscription-userinfo.conf',
          '/etc/systemd/system/traffic-update.service.d/migrate.conf',
          '/etc/letsencrypt/renewal-hooks/deploy/lightsail-vpn-nginx',
          *['/etc/systemd/system/'+u for u in up.UNITS if u!='hysteria-server.service']]
MANAGED_UNITS=[u for u in up.UNITS if u!='hysteria-server.service']
STATE_UNITS=['hysteria-server.service','nginx',*MANAGED_UNITS,'snap.certbot.renew.timer']


class Migration:
    def __init__(self,root=Path('/'),repo=REPO,runner=None):
        self.root=Path(root); self.repo=Path(repo); self.updater=up.Updater(root,repo,runner)
        self.runner=runner or self.updater.run
        self.adopted_sites=[]

    def path(self,path): return self.updater.path(path)

    def config(self):
        try:
            import yaml
        except ImportError:
            raise ValueError('请先安装解析依赖：sudo apt-get install python3-yaml') from None
        try:
            data=yaml.safe_load(self.path('/etc/hysteria/config.yaml').read_text())
            assert isinstance(data,dict)
            assert data['auth']['type']=='password'
            assert data['obfs']['type']=='salamander'
            for value in (data['auth']['password'],data['obfs']['salamander']['password']):
                assert isinstance(value,str) and value and not any(c in value for c in '\\"\r\n')
            assert data.get('listen',':443') in (':443','0.0.0.0:443','[::]:443')
            assert data['tls']['cert']=='/etc/hysteria/certs/server.crt'
            assert data['tls']['key']=='/etc/hysteria/certs/server.key'
            stats=data.get('trafficStats')
            if stats:
                assert isinstance(stats,dict) and stats.get('listen')=='127.0.0.1:25413'
                assert isinstance(stats.get('secret'),str) and stats['secret']
            return data
        except Exception:
            # YAML 异常常含配置原文，绝不能直接输出。
            raise ValueError('旧配置格式不在安全迁移范围：需要 password + Salamander、443 端口和标准证书路径；未修改配置') from None

    def public_yaml(self):
        root=self.path('/var/www/html')
        if not root.exists(): return []
        files=[p for p in root.iterdir() if p.suffix.lower() in ('.yaml','.yml')]
        if any(p.is_symlink() or not p.is_file() for p in files):
            raise ValueError('公开目录有订阅符号链接或非普通 YAML 文件，需先人工核对')
        return files

    def nginx_sites(self,a):
        reports=layout.inspect_sites(self.root)
        selected=list(dict.fromkeys(getattr(a,'adopt_nginx_site',[]) or []))
        if any(not layout.NAME.fullmatch(name) or name=='default' for name in selected):
            raise ValueError('--adopt-nginx-site 需填写诊断列出的非 default 站点文件名')
        by_name={report['name']:report for report in reports}
        for name in selected:
            if name not in by_name or not by_name[name]['candidate']:
                raise ValueError('所选站点不是受支持的静态 VPN 候选；先运行 sudo bash migrate.sh --inspect-nginx 核对，不会强行覆盖')
        remaining=[r for r in reports if r['name']!='default' and r['name'] not in selected]
        if remaining:
            names=', '.join(r['name'] if layout.NAME.fullmatch(r['name']) else '<非标准文件名>' for r in remaining)
            raise ValueError('额外启用站点尚未确认：'+names+'；先运行 sudo bash migrate.sh --inspect-nginx，候选站点可显式 --adopt-nginx-site 文件名')
        self.adopted_sites=selected

    def preflight(self,a):
        boot.validate(a)
        if self.root==Path('/'):
            for command in ('nginx','curl','openssl','systemctl'):
                if not shutil.which(command): raise ValueError('缺少迁移依赖：'+command+'；先安装后重试')
            if not self.path('/snap/bin/certbot').exists() and not shutil.which('snap'):
                raise ValueError('缺少 Certbot/snap；先安装 snapd 后重试')
        for path in ('/etc/hysteria/config.yaml','/etc/systemd/system/hysteria-server.service',
                     '/etc/hysteria/certs/server.crt','/etc/hysteria/certs/server.key','/usr/local/bin/hysteria'):
            if not self.path(path).is_file(): raise ValueError('缺少基础旧服务文件：'+path)
        # 此入口只处理用户报告的无本项目订阅状态的早期安装，不猜测半迁移状态。
        for path in ('/etc/hysteria/.sub-state','/etc/hysteria/.sub-node','/etc/hysteria/.sub-token',
                     '/etc/nginx/sites-available/vpn-subscription'):
            if self.path(path).exists(): raise ValueError('已有订阅状态或站点，不能用基础迁移入口重复接管；使用 update.sh 或先核对布局')
        if self.path('/var/www/html/'+a.name+'.yaml').exists():
            raise ValueError('新订阅文件名已存在；换一个 --name，不覆盖旧订阅')
        self.nginx_sites(a)
        configs=list(self.path('/etc/nginx/conf.d').glob('*.conf'))
        if self.path('/etc/nginx/nginx.conf').exists(): configs.append(self.path('/etc/nginx/nginx.conf'))
        for file in configs:
            if re.search(r'(?m)^\s*server\s*\{',file.read_text()):
                raise ValueError('Nginx 全局配置或 conf.d 有额外 server，需先核对路由，不能自动接管')
        for path in SNAPSHOT:
            p=self.path(path)
            if p.is_symlink() and path not in ('/etc/nginx/sites-enabled/default','/etc/nginx/sites-enabled/vpn-subscription'):
                raise ValueError('迁移目标为符号链接，需先核对：'+path)
        cfg=self.config()
        self.public_yaml()
        user=self.runner(['systemctl','show','hysteria-server.service','-p','User','--value']).strip()
        if user not in ('','root','hysteria'): raise ValueError('Hysteria 使用自定义运行用户，需先核对文件权限，不能自动迁移')
        if self.root==Path('/'):
            start=self.runner(['systemctl','show','hysteria-server.service','-p','ExecStart','--value'])
            if '/usr/local/bin/hysteria' not in start or '/etc/hysteria/config.yaml' not in start:
                raise ValueError('Hysteria 服务使用非标准启动路径，需先核对，不自动迁移')
        # 原版 sub 根据自签证书 SAN 获取节点 IP；不能悄悄替换现有证书。
        san=self.runner(['openssl','x509','-in',str(self.path('/etc/hysteria/certs/server.crt')),'-noout','-ext','subjectAltName'])
        if not re.search(r'IP Address:'+re.escape(a.ip)+r'(?:[\s,]|$)',san):
            raise ValueError('--ip 与旧自签证书 SAN 不一致；先核对公网 IP，不自动重签旧证书')
        if not a.upgrade_hysteria:
            version=self.runner([str(self.path('/usr/local/bin/hysteria')),'version'])
            if not re.search(r'v?2\.12\.0\b',version):
                raise ValueError('为保证统计 API 兼容，旧版本需显式 --upgrade-hysteria 升级为已验证的 2.12.0')
        return cfg

    def report(self,a):
        self.preflight(a)
        print('基础旧安装迁移检查通过。--apply 将执行：')
        print('  备份并替换旧 sub/Geo 程序，补装域名统计和 AWS 套餐统计')
        print('  保留代理密码、混淆密码、TLS 证书及所有额外 Hysteria 配置；增加本地 trafficStats')
        print('  生成新的订阅路径/token；客户端链接需要重新导入')
        if self.public_yaml():
            print('  发现旧公开 YAML 订阅；需加 --archive-old-subscriptions 移入 root 私密备份，防止旧鉴权被绕过')
        for name in self.adopted_sites:
            print('  备份并停用你选定的旧静态 VPN 站点：'+name+'；源配置或私密备份保留，失败时恢复启用项')
        print('  接管默认 Nginx 站点，配置新 IP HTTPS 订阅，启用原版四次/日 Geo 定时器')
        print('  下载规则，重启 Hysteria 和域名采集；代理会短暂断线')
        print('  AWS 凭据不读取/复制，套餐统计可能仍需你手动配置只读身份')
        if a.upgrade_hysteria: print('  验证官方 SHA256 后升级 Hysteria 到 2.12.0')
        print('不会执行迁移。确认计划后加 --apply --replace-legacy-code。')

    def backup(self,a):
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4)
        directory=self.path('/var/backups/lightsail-vpn-migration')/stamp
        directory.mkdir(parents=True,mode=0o700); directory.parent.chmod(0o700)
        targets=SNAPSHOT+['/var/www/html/'+a.name+'.yaml']
        targets += ['/var/www/html/'+p.name for p in self.public_yaml()]
        targets += ['/etc/nginx/sites-enabled/'+name for name in self.adopted_sites]
        manifest={'files':[],'units':{},'status':'prepared','subscription':a.name,'adopted_sites':list(self.adopted_sites)}
        for target in targets:
            if target=='/usr/local/bin/hysteria' and not a.upgrade_hysteria: continue
            path=self.path(target); entry={'target':target,'exists':path.exists() or path.is_symlink()}
            if path.is_symlink(): entry['link']=os.readlink(path)
            elif path.exists():
                st=path.stat(); name='file-'+str(len(manifest['files']))
                self.updater.atomic(directory/name,path.read_bytes(),0o600)
                entry.update(backup=name,mode=st.st_mode&0o777,uid=st.st_uid,gid=st.st_gid)
            manifest['files'].append(entry)
        for unit in STATE_UNITS:
            try: enabled=self.runner(['systemctl','is-enabled',unit]).strip()
            except RuntimeError: enabled='disabled'
            present=unit in ('hysteria-server.service','nginx') or self.path('/etc/systemd/system/'+unit).is_file()
            if unit=='snap.certbot.renew.timer':
                try: present=self.runner(['systemctl','show','-p','LoadState','--value',unit]).strip()=='loaded'
                except RuntimeError: present=False
            manifest['units'][unit]={'active':self.updater.active(unit),'enabled':enabled,'present':present}
        self.updater.atomic(directory/'manifest.json',json.dumps(manifest).encode(),0o600)
        return directory,manifest

    def write(self,path,data,mode=0o600,group=None):
        gid=grp.getgrnam(group).gr_gid if group else None
        self.updater.atomic(self.path(path),data.encode() if isinstance(data,str) else data,mode,
                            0 if group else None,gid)

    def config_with_stats(self,cfg):
        import yaml
        class Quoted(str): pass
        class Dumper(yaml.SafeDumper): pass
        Dumper.add_representer(Quoted,lambda dumper,value:dumper.represent_scalar('tag:yaml.org,2002:str',str(value),style='"'))
        secret_file=self.path('/etc/hysteria/.traffic-stats-secret')
        stats=cfg.get('trafficStats') or {'listen':'127.0.0.1:25413','secret':secrets.token_hex(32)}
        if secret_file.exists() and secret_file.read_text().strip()!=stats['secret']:
            raise ValueError('现有统计 secret 与 Hysteria 不一致，停止迁移，不输出秘密')
        # 保留原有配置语义；只规范原版 sub 所需的密码引号/区块顺序，并加入统计 API。
        result={key:cfg[key] for key in ('listen','tls','auth','obfs') if key in cfg}
        result.update({k:v for k,v in cfg.items() if k not in result and k!='trafficStats'})
        result['auth']['password']=Quoted(result['auth']['password'])
        result['obfs']['salamander']['password']=Quoted(result['obfs']['salamander']['password'])
        result['trafficStats']=dict(stats,secret=Quoted(stats['secret']))
        return yaml.dump(result,Dumper=Dumper,allow_unicode=True,sort_keys=False),stats['secret']

    def install_binary(self):
        arch={'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
        if not arch: raise ValueError('仅支持 amd64/arm64')
        with tempfile.TemporaryDirectory(prefix='vpn-migration-release-') as tmp:
            base='https://github.com/apernet/hysteria/releases/download/app/v2.12.0/'
            name='hysteria-linux-'+arch
            for file in (name,'hashes.txt'):
                self.runner(['curl','-fsSL','--retry','3','--connect-timeout','15','--max-time','600',base+file,'-o',tmp+'/'+file])
            self.runner(['python3',str(self.repo/'scripts/verify-release.py'),tmp,name])
            self.write('/usr/local/bin/hysteria',Path(tmp,name).read_bytes(),0o755)

    def probe_stats(self,secret):
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(5):
            try:
                request=urllib.request.Request('http://127.0.0.1:25413/dump/streams',headers={'Authorization':secret})
                with opener.open(request,timeout=3) as response: data=json.load(response)
                if isinstance(data,dict) and (data.get('streams') is None or isinstance(data['streams'],list)): return
            except Exception: pass
            time.sleep(0.5)
        raise RuntimeError('本地统计 API 验证失败')

    def apply(self,a):
        cfg=self.preflight(a)
        if not a.replace_legacy_code: raise ValueError('迁移会替换未知旧 sub/Geo，请显式加 --replace-legacy-code；原文件将先私密备份')
        public=self.public_yaml()
        if public and not a.archive_old_subscriptions:
            raise ValueError('发现旧公开 YAML；需显式 --archive-old-subscriptions 移入私密备份，避免绕过旧订阅鉴权')
        config,stats_secret=self.config_with_stats(cfg)
        for unit in ('geo-update.service','traffic-update.service','domain-usage-prune.service'):
            if self.updater.active(unit): raise ValueError(unit+' 正在运行，请稍后重试')
        directory,manifest=self.backup(a)
        print('迁移备份 ID：'+directory.name)
        stage='依赖与目录准备'
        try:
            print('1/6 准备依赖与目录')
            os.environ.setdefault('DEBIAN_FRONTEND','noninteractive')
            if manifest['units']['geo-update.timer']['active']:
                self.runner(['systemctl','stop','geo-update.timer'])
            for name in self.adopted_sites: self.path('/etc/nginx/sites-enabled/'+name).unlink()
            if self.adopted_sites: print('已备份并停用选定旧 VPN 站点；源配置或私密备份可用于恢复')
            for file in public: file.unlink()
            if public: print('旧公开订阅已移入本次 root 私密备份；可恢复，不再公开提供旧文件')
            self.runner(['apt-get','install','-y','python3-boto3'])
            try: self.runner(['id','hysteria'])
            except RuntimeError: self.runner(['useradd','--system','--no-create-home','--shell','/usr/sbin/nologin','hysteria'])
            try: self.runner(['id','usage-tracker'])
            except RuntimeError: self.runner(['useradd','--system','--no-create-home','--shell','/usr/sbin/nologin','usage-tracker'])
            for path,mode in [('/var/www/html/geo',0o755),('/var/lib/geo-update',0o700),('/var/lib/traffic',0o700),
                              ('/var/lib/domain-usage',0o750),('/etc/nginx/.sub-backup',0o700)]:
                self.path(path).mkdir(parents=True,exist_ok=True)
            self.runner(['chown','usage-tracker:usage-tracker',str(self.path('/var/lib/domain-usage'))])
            self.path('/etc/nginx/.sub-backup').chmod(0o700)
            for path in ('/var/log/geo-update.log','/var/log/traffic-update.log','/var/log/domain-usage.log'):
                if not self.path(path).exists(): self.write(path,'',0o600)
            self.runner(['chown','usage-tracker:usage-tracker',str(self.path('/var/log/domain-usage.log'))])
            self.path('/var/log/domain-usage.log').chmod(0o640)
            stage='程序与配置安装'
            print('2/6 安装程序，保留代理密码与证书，增加本地统计配置')
            for target,(source,mode) in up.FILES.items(): self.write(target,(self.repo/source).read_bytes(),mode)
            for unit in MANAGED_UNITS:
                self.write('/etc/systemd/system/'+unit,(self.repo/'systemd'/unit).read_bytes(),0o644)
            if a.upgrade_hysteria: self.install_binary()
            self.write('/etc/hysteria/config.yaml',config,0o640,'hysteria')
            self.write('/etc/hysteria/.traffic-stats-secret',stats_secret+'\n',0o640,'usage-tracker')
            self.write('/etc/hysteria/.sub-state',f'/var/www/html/{a.name}.yaml\n')
            self.write('/etc/hysteria/.sub-node',a.node+'\n')
            self.write('/etc/hysteria/.sub-token',secrets.token_hex(32)+'\n')
            self.write('/etc/systemd/system/traffic-update.service.d/migrate.conf',
                       f'[Service]\nEnvironment=LS_REGION={a.region}\nEnvironment=LS_INSTANCE_NAME={a.instance_name}\nEnvironment=LS_PRIVATE_IP=\nEnvironment=LS_PUBLIC_IP={a.ip}\n',0o644)
            if not self.path('/etc/nginx/subscription-userinfo.conf').exists(): self.write('/etc/nginx/subscription-userinfo.conf','',0o644)
            self.runner(['systemctl','daemon-reload'])
            self.runner(['systemctl','start','nginx'])
            stage='Nginx 与 IP HTTPS'
            print('3/6 更新订阅站点，申请/确认 IP HTTPS 证书')
            # sub 输出含 token，捕获但不打印、不写入迁移日志。
            self.runner([str(self.path('/usr/local/bin/sub')),'update'])
            if not self.path('/snap/bin/certbot').exists(): self.runner(['snap','install','--classic','certbot'])
            self.write('/etc/letsencrypt/renewal-hooks/deploy/lightsail-vpn-nginx',
                       (self.repo/'scripts/reload-nginx.sh').read_bytes(),0o755)
            if boot.reusable_certificate(a.ip,self.path('/etc/letsencrypt/live'),self.runner):
                print('复用现有有效 IP HTTPS 证书，不修改账户邮箱')
            else:
                self.runner(boot.certbot_command(a,self.path('/var/www/html'),self.path('/snap/bin/certbot')))
            for name in ('fullchain.pem','privkey.pem'):
                if not self.path('/etc/letsencrypt/live/'+a.ip+'/'+name).is_file(): raise RuntimeError('HTTPS 证书未生成')
            self.runner([str(self.path('/usr/local/bin/sub')),'update'])
            stage='Geo 数据下载'
            print('4/6 下载全部 Geo 和 CN-only MRS，可能需要几分钟')
            self.runner([str(self.path('/usr/local/bin/geo-update')),'update'])
            stage='代理与本地统计验证'
            print('5/6 重启代理并验证本地统计 API')
            self.runner(['systemctl','restart','hysteria-server.service'])
            self.runner(['systemctl','is-active','--quiet','hysteria-server.service'])
            self.probe_stats(stats_secret)
            self.runner(['systemctl','enable','--now','domain-usage.service','domain-usage.timer','geo-update.timer','traffic-update.timer','snap.certbot.renew.timer'])
            self.runner(['systemctl','is-active','--quiet','domain-usage.service'])
            manifest['status']='complete'
            self.updater.atomic(directory/'manifest.json',json.dumps(manifest).encode(),0o600)
            self.updater.save_registry()
        except Exception:
            try: self.restore(directory,manifest)
            except Exception: raise RuntimeError('迁移失败且自动恢复未完成；使用私密备份人工恢复，不要重复运行 deploy.sh') from None
            raise RuntimeError('迁移失败（'+stage+'），已尝试恢复原程序/配置/服务状态；备份 ID：'+directory.name) from None
        print('6/6 尝试获取 AWS 套餐统计（需本机只读身份）')
        try: self.runner(['systemctl','start','--wait','traffic-update.service'])
        except RuntimeError: print('提示：AWS 套餐统计未就绪；按 README 在本机手动配置只读身份后重试，VPN 不受此影响')
        print('迁移完成。执行 sudo sub url 获取新链接；客户端需导入并验证。')

    def restore(self,directory,manifest):
        name=manifest['subscription']
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}',name): raise ValueError('备份订阅标识无效')
        allowed=set(SNAPSHOT)|{'/var/www/html/'+name+'.yaml'}
        adopted=manifest.get('adopted_sites',[])
        if any(not isinstance(site,str) or not layout.NAME.fullmatch(site) or site=='default' for site in adopted):
            raise ValueError('备份站点标识不合法')
        allowed.update('/etc/nginx/sites-enabled/'+site for site in adopted)
        for entry in manifest['files']:
            target=Path(entry['target'])
            archived=target.parent==Path('/var/www/html') and target.suffix.lower() in ('.yaml','.yml')
            if entry['target'] not in allowed and not archived: raise ValueError('备份目标不合法')
        for unit in [*MANAGED_UNITS,'snap.certbot.renew.timer']:
            # 新增组件原本不存在时 systemctl 会返回非零；仍必须继续恢复文件。
            for action in ('stop','disable'):
                try: self.runner(['systemctl',action,unit])
                except RuntimeError: pass
        for entry in reversed(manifest['files']):
            path=self.path(entry['target'])
            if path.is_symlink(): path.unlink()
            if not entry['exists']: path.unlink(missing_ok=True)
            elif 'link' in entry:
                path.unlink(missing_ok=True); path.symlink_to(entry['link'])
            else:
                file=entry['backup']
                if not re.fullmatch(r'file-\d+',file): raise ValueError('备份文件名无效')
                self.updater.atomic(path,(directory/file).read_bytes(),entry['mode'],entry['uid'],entry['gid'])
        self.runner(['systemctl','daemon-reload'])
        for unit,state in manifest['units'].items():
            if unit not in STATE_UNITS: raise ValueError('备份单元不合法')
            if not state['present']: continue
            if unit not in ('hysteria-server.service','nginx'):
                command=['systemctl','enable' if state['enabled'].startswith('enabled') else 'disable']
                if state['enabled']=='enabled-runtime': command.append('--runtime')
                self.runner([*command,unit])
            if state['active']: self.runner(['systemctl','restart',unit])
            else: self.runner(['systemctl','stop',unit])
        manifest['status']='rolled-back'
        self.updater.atomic(directory/'manifest.json',json.dumps(manifest).encode(),0o600)


def main():
    if '--inspect-nginx' in sys.argv[1:]:
        if sys.argv[1:]!=['--inspect-nginx']: raise ValueError('--inspect-nginx 请单独运行，不需要 IP、邮箱或其他迁移参数')
        if os.geteuid()!=0: raise ValueError('请用 sudo bash migrate.sh --inspect-nginx 运行')
        reports=layout.inspect_sites()
        print(json.dumps({'sites':reports,'note':'candidate 仅表示受限静态 VPN 候选，仍需你确认用途；不输出 token、域名、订阅路径或配置原文'},ensure_ascii=False,indent=2))
        return
    p=boot.parser()
    p.description='基础旧安装迁移：保留认证和证书，补齐订阅与统计；默认只检查'
    action=p.add_mutually_exclusive_group()
    action.add_argument('--check',action='store_true')
    action.add_argument('--apply',action='store_true')
    p.add_argument('--replace-legacy-code',action='store_true',help='显式同意备份并替换旧 sub/Geo 程序')
    p.add_argument('--upgrade-hysteria',action='store_true',help='备份原二进制并升级到 2.12.0')
    p.add_argument('--archive-old-subscriptions',action='store_true',help='将旧公开 YAML 订阅归档至 root 私密备份，旧链接需替换')
    p.add_argument('--adopt-nginx-site',action='append',default=[],metavar='NAME',help='显式备份并停用诊断认可的旧静态 VPN 站点，可重复指定')
    p.add_argument('--inspect-nginx',action='store_true',help='单独运行，仅输出脱敏 Nginx 路由结构，不需要其他参数')
    a=p.parse_args()
    if a.tls or a.validate_only: raise ValueError('不支持初始化入口的内部参数')
    if os.geteuid()!=0: raise ValueError('请用 sudo bash migrate.sh 运行')
    m=Migration()
    if not a.apply: m.report(a); return
    with open('/run/lightsail-vpn-deploy.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        m.apply(a)


if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError,OSError):
        # CLI 仅输出经过人工设计的错误；底层解析异常不能泄露原文。
        error=sys.exc_info()[1]
        if isinstance(error,(ValueError,RuntimeError)): sys.exit('error: '+str(error))
        sys.exit('error: 文件/权限操作失败；检查本机日志和私密备份，不要分享密钥')
