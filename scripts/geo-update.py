#!/usr/bin/env python3
"""CN-only MRS 缓存：每日定时，验证 Zstd 流，单文件原子发布，失败保留旧数据。"""
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

GEO = Path('/var/lib/lightsail-vpn/geo')
FILES = {'geosite-cn.mrs': ('geosite', 1024), 'geoip-cn.mrs': ('geoip', 1024)}
SOURCES = ['https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta',
           'https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@meta']


def update():
    GEO.mkdir(parents=True, exist_ok=True)
    failed = False
    with (GEO / '.update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('规则更新已在运行'); return
        for name, (kind, minimum) in FILES.items():
            fd, tmp = tempfile.mkstemp(prefix='.download-', dir=GEO)
            os.close(fd)
            try:
                for base in SOURCES:
                    result = subprocess.run(['curl', '-fsSL', '--retry', '2', '--connect-timeout', '15',
                                             '--max-time', '300', '-o', tmp, f'{base}/geo/{kind}/cn.mrs'],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if result.returncode or Path(tmp).stat().st_size < minimum:
                        continue
                    with open(tmp, 'rb') as stream:
                        if stream.read(4) != bytes.fromhex('28b52ffd'):
                            continue
                    if subprocess.run(['zstd', '-t', '-q', tmp], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL).returncode:
                        continue
                    os.chmod(tmp, 0o644)
                    os.replace(tmp, GEO / name)
                    print(f'{name}: 更新成功，{(GEO / name).stat().st_size} bytes')
                    break
                else:
                    failed = True
                    print(f'{name}: 所有来源失败，保留旧文件', file=sys.stderr)
            finally:
                Path(tmp).unlink(missing_ok=True)
    if failed:
        raise RuntimeError('规则未全部更新，请检查网络或 journalctl -u geo-update')


def status():
    for name in FILES:
        p = GEO / name
        if p.exists():
            print(f'{name}: {p.stat().st_size} bytes, SHA256={hashlib.sha256(p.read_bytes()).hexdigest()}')
        else:
            print(f'{name}: 缺失')
    subprocess.run(['systemctl', 'list-timers', 'geo-update.timer', '--no-pager'])


if __name__ == '__main__':
    try:
        if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] not in ('update', 'status')):
            raise ValueError('用法: sudo geo-update update|status')
        action = sys.argv[1] if len(sys.argv) == 2 else 'update'
        if action == 'update' and os.geteuid() != 0:
            raise ValueError('请用 sudo 运行')
        (update if action == 'update' else status)()
    except (RuntimeError, ValueError, OSError) as exc:
        sys.exit(f'error: {exc}')
