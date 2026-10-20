#!/usr/bin/env python3
"""原项目旧安装的离线代码更新器。不读取密码、不生成订阅、不初始化服务。"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

REPO=Path(__file__).resolve().parents[1]
BACKUPS='/var/backups/lightsail-vpn-update'
FILES={
    '/usr/local/bin/sub': ('scripts/sub.sh',0o755),
    '/usr/local/bin/geo-update': ('scripts/geo-update.sh',0o755),
    '/usr/local/bin/domain-usage': ('scripts/domain-usage.py',0o755),
    '/usr/local/lib/lightsail-vpn/traffic-update.py': ('scripts/traffic-update.py',0o755),
    '/usr/local/bin/traffic-update': ('scripts/traffic-launcher.py',0o755),
}
REQUIRED=(
    '/etc/hysteria/config.yaml', '/etc/hysteria/.sub-state', '/etc/hysteria/.sub-node',
    '/etc/hysteria/.sub-token', '/etc/hysteria/.traffic-stats-secret',
    '/etc/nginx/sites-available/vpn-subscription',
    '/etc/systemd/system/traffic-update.service.d/migrate.conf',
)
UNITS=('hysteria-server.service','geo-update.service','geo-update.timer',
       'traffic-update.service','traffic-update.timer','domain-usage.service',
       'domain-usage-prune.service','domain-usage.timer')
CORE_REQUIRED=('/etc/hysteria/config.yaml','/etc/systemd/system/hysteria-server.service')
COMPONENTS={
    '订阅管理': (('/usr/local/bin/sub','/etc/hysteria/.sub-state','/etc/hysteria/.sub-node',
                  '/etc/hysteria/.sub-token','/etc/nginx/sites-available/vpn-subscription'),
                 ('/usr/local/bin/sub',),()),
    'Geo 缓存': (('/usr/local/bin/geo-update',),('/usr/local/bin/geo-update',),
                 ('geo-update.service','geo-update.timer')),
    '域名统计': (('/usr/local/bin/domain-usage','/etc/hysteria/.traffic-stats-secret',
                  '/etc/systemd/system/domain-usage.service'),('/usr/local/bin/domain-usage',),
                 ('domain-usage.service','domain-usage-prune.service','domain-usage.timer')),
    'AWS 套餐统计': (('/usr/local/bin/traffic-update',
                     '/etc/systemd/system/traffic-update.service.d/migrate.conf',
                     '/etc/systemd/system/traffic-update.service'),
                    ('/usr/local/lib/lightsail-vpn/traffic-update.py','/usr/local/bin/traffic-update'),
                    ('traffic-update.service','traffic-update.timer')),
}


def digest(data): return hashlib.sha256(data).hexdigest()


class Updater:
    def __init__(self,root=Path('/'),repo=REPO,runner=None):
        self.root=Path(root); self.repo=Path(repo); self.runner=runner or self.run
        self.notices=[]; self.blocked=[]

    def path(self,absolute): return self.root/absolute.lstrip('/')

    @staticmethod
    def run(args):
        result=subprocess.run(args,capture_output=True,text=True)
        if result.returncode: raise RuntimeError(f'{args[0]} 操作失败，请查看对应服务日志')
        return result.stdout

    def active(self,unit):
        try: self.runner(['systemctl','is-active','--quiet',unit]); return True
        except RuntimeError: return False

    def baseline(self):
        return {name:checksum for checksum,name in
                (line.split(maxsplit=1) for line in (self.repo/'baseline.sha256').read_text().splitlines())}

    def known(self):
        hashes=set(self.baseline().values())
        hashes.update(digest((self.repo/source).read_bytes()) for source,_ in FILES.values())
        legacy=self.repo/'config/legacy-unit-hashes.json'
        if legacy.exists(): hashes.update(json.loads(legacy.read_text()).values())
        # 上一次成功更新登记的文件哈希，允许从后续仓库版本继续增量更新。
        registry=self.path('/var/lib/lightsail-vpn-updater/installed.json')
        if registry.exists(): hashes.update(json.loads(registry.read_text()).values())
        return hashes

    def targets(self,units=False):
        self.notices=[]
        missing=[p for p in CORE_REQUIRED if not self.path(p).is_file()]
        if missing: raise ValueError('未发现预期的 Hysteria 基础安装，缺少：'+', '.join(missing))
        result={}
        if units:
            result['/etc/systemd/system/hysteria-server.service']=('systemd/hysteria-server.service',0o644)
        for name,(required,programs,unit_names) in COMPONENTS.items():
            missing=[p for p in required if not self.path(p).is_file()]
            if missing:
                self.notices.append(f'{name}：未安装或布局不兼容，跳过并保留现场；缺少 '+', '.join(missing))
                continue
            if name=='AWS 套餐统计':
                import runpy
                settings=runpy.run_path(str(self.repo/'scripts/traffic-launcher.py'))['settings']
                try: settings(self.path('/etc/systemd/system/traffic-update.service.d/migrate.conf'))
                except (ValueError,OSError):
                    self.notices.append('AWS 套餐统计：实例参数不完整，跳过；需先配置 LS_REGION/LS_INSTANCE_NAME')
                    continue
            result.update({p:FILES[p] for p in programs})
            if units:
                for unit in unit_names:
                    path='/etc/systemd/system/'+unit
                    if self.path(path).is_file(): result[path]=('systemd/'+unit,0o644)
                    else: self.notices.append(f'{path} 不存在，不自动创建或启用')
        return result

    def plan(self,units=False,tolerate_unknown=False):
        known=self.known(); targets=self.targets(units); self.blocked=[]
        changes=[]
        for target,(source,mode) in targets.items():
            path=self.path(target); data=(self.repo/source).read_bytes()
            if path.is_symlink(): raise ValueError(f'{target} 是符号链接，拒绝覆盖')
            before=path.read_bytes() if path.exists() else None
            if before==data: continue
            if before is not None and digest(before) not in known:
                message=f'{target} 是未收录的旧版本或有本地改动；不会覆盖，需先核对源码'
                if not tolerate_unknown: raise ValueError(message)
                self.blocked.append(message); continue
            changes.append((target,data,mode))
        return changes

    def atomic(self,path,data,mode,uid=None,gid=None):
        path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix='.vpn-update-',dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as out:
                out.write(data); out.flush(); os.fsync(out.fileno())
            os.chmod(tmp,mode)
            if uid is not None: os.chown(tmp,uid,gid)
            os.replace(tmp,path)
        finally: Path(tmp).unlink(missing_ok=True)

    def save_registry(self):
        values={}
        known=self.known()
        for target,(source,_) in self.targets(units=True).items():
            if not self.path(target).is_file(): continue
            data=self.path(target).read_bytes(); checksum=digest(data)
            # 没有更新的自定义单元不能被登记成“受管理”，否则下次会误覆盖。
            if checksum in known or data==(self.repo/source).read_bytes():
                values[target]=checksum
        self.atomic(self.path('/var/lib/lightsail-vpn-updater/installed.json'),json.dumps(values).encode(),0o600)

    def apply(self,units=False):
        changes=self.plan(units)
        for note in self.notices: print('提示：'+note)
        if not changes: print('已识别组件无需更新；跳过的缺失/不兼容组件没有安装或迁移'); return None
        for service in ('traffic-update.service','geo-update.service','domain-usage-prune.service'):
            if self.active(service): raise ValueError(f'{service} 正在运行，请等它完成后重试')
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4)
        directory=self.path(BACKUPS)/stamp
        directory.mkdir(parents=True,mode=0o700)
        self.path(BACKUPS).chmod(0o700)
        manifest={'files':[],'active':{},'status':'prepared'}
        for target,data,mode in changes:
            path=self.path(target); entry={'target':target,'after':digest(data),'exists':path.exists()}
            if path.exists():
                st=path.stat(); entry.update(mode=st.st_mode&0o777,uid=st.st_uid,gid=st.st_gid)
                name='file-'+str(len(manifest['files'])); entry['backup']=name
                self.atomic(directory/name,path.read_bytes(),0o600)
            manifest['files'].append(entry)
        domain_changed=any(p=='/usr/local/bin/domain-usage' for p,_,_ in changes)
        unit_changed=any(p.startswith('/etc/systemd/system/') for p,_,_ in changes)
        if domain_changed: manifest['active']['domain-usage.service']=self.active('domain-usage.service')
        for target,_,_ in changes:
            if target.startswith('/etc/systemd/system/'):
                unit=Path(target).name
                manifest['active'][unit]=self.active(unit)
        self.atomic(directory/'manifest.json',json.dumps(manifest).encode(),0o600)
        try:
            for target,data,mode in changes: self.atomic(self.path(target),data,mode)
            if unit_changed: self.runner(['systemctl','daemon-reload'])
            for unit,was_active in manifest['active'].items():
                if was_active:
                    self.runner(['systemctl','restart',unit])
                    self.runner(['systemctl','is-active','--quiet',unit])
            manifest['status']='complete'
            self.atomic(directory/'manifest.json',json.dumps(manifest).encode(),0o600)
            self.save_registry()
        except Exception:
            self.restore(directory,manifest)
            raise
        print(f'更新完成，备份 ID：{stamp}')
        return stamp

    def restore(self,directory,manifest):
        allowed=set(FILES)|{f'/etc/systemd/system/{u}' for u in UNITS}
        for entry in manifest['files']:
            target=entry['target']
            if target not in allowed: raise ValueError('备份包含非更新目标，拒绝恢复')
            path=self.path(target)
            if entry['exists']:
                name=entry['backup']
                if not re.fullmatch(r'file-\d+',name): raise ValueError('备份文件名无效')
                self.atomic(path,(directory/name).read_bytes(),entry['mode'],entry['uid'],entry['gid'])
            else: path.unlink(missing_ok=True)
        if any(e['target'].startswith('/etc/systemd/system/') for e in manifest['files']):
            self.runner(['systemctl','daemon-reload'])
        for unit,was_active in manifest['active'].items():
            if was_active: self.runner(['systemctl','restart',unit])
        manifest['status']='rolled-back'
        self.atomic(directory/'manifest.json',json.dumps(manifest).encode(),0o600)
        self.save_registry()

    def rollback(self,stamp):
        if not re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{8}',stamp): raise ValueError('备份 ID 无效')
        directory=self.path(BACKUPS)/stamp
        manifest=json.loads((directory/'manifest.json').read_text())
        if manifest['status']!='complete': raise ValueError('只能回退已成功完成且尚未回退的更新')
        allowed=set(FILES)|{f'/etc/systemd/system/{u}' for u in UNITS}
        for entry in manifest['files']:
            if entry['target'] not in allowed: raise ValueError('备份包含非更新目标，拒绝恢复')
            path=self.path(entry['target'])
            if not path.exists() or digest(path.read_bytes())!=entry['after']:
                raise ValueError('更新后程序文件又有变化，拒绝覆盖；请按时间逆序回退')
        self.restore(directory,manifest)
        print('已恢复更新前程序；原配置、密钥及统计数据未改动')


def main():
    p=argparse.ArgumentParser(description='按组件识别旧安装并增量更新；默认仅检查，不读取/重建秘密配置')
    action=p.add_mutually_exclusive_group()
    action.add_argument('--check',action='store_true',help='只读检查，默认行为')
    action.add_argument('--apply',action='store_true',help='备份并更新程序，不重装 Hysteria/Nginx')
    action.add_argument('--rollback',metavar='ID',help='回退指定更新的程序文件')
    p.add_argument('--update-units',action='store_true',help='同时更新可识别的原版 systemd 单元，可能短暂断线；默认保留本机单元')
    a=p.parse_args()
    if os.geteuid()!=0: raise ValueError('请用 sudo bash update.sh 运行')
    updater=Updater()
    if not a.apply and not a.rollback:
        changes=updater.plan(a.update_units,tolerate_unknown=True)
        for note in updater.notices: print('提示：'+note)
        for note in updater.blocked: print('待确认：'+note)
        print('基础服务已识别；可更新项目：' if changes else '基础服务已识别；当前没有可自动更新的代码')
        for path,_,_ in changes: print('  '+path)
        if updater.blocked: print('存在未识别代码，--apply 会停止；请先核对，不能视为完整兼容通过。')
        print('原配置、订阅、密钥、Geo 缓存、SQLite 和 AWS 凭据均不变。')
        return
    lock=Path('/run/lightsail-vpn-deploy.lock')
    with lock.open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.rollback: updater.rollback(a.rollback)
        else: updater.apply(a.update_units)


if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError,OSError,KeyError) as exc: sys.exit(f'error: {exc}')
