#!/usr/bin/env python3
"""让手动 sub traffic 与 systemd 使用同一地区/实例参数，核心统计源码保持原版。"""
import os
from pathlib import Path
import shlex
import sys

ALLOWED={'LS_REGION','LS_INSTANCE_NAME','LS_PRIVATE_IP','LS_PUBLIC_IP','LS_TOTAL_GB'}


def settings(path):
    result={}
    for line in Path(path).read_text().splitlines():
        if not line.startswith('Environment='): continue
        for item in shlex.split(line.removeprefix('Environment=')):
            name,separator,value=item.partition('=')
            if separator and name in ALLOWED: result[name]=value
    if not result.get('LS_REGION') or not result.get('LS_INSTANCE_NAME'):
        raise ValueError('流量统计地区/实例名未配置')
    return result


if __name__=='__main__':
    try:
        os.environ.update(settings(os.environ.get('TRAFFIC_SERVICE_OVERRIDE','/etc/systemd/system/traffic-update.service.d/migrate.conf')))
    except (OSError,ValueError):
        sys.exit('error: 流量统计实例配置缺失；请检查 README 和 migrate.conf')
    os.execv('/usr/bin/python3',['python3','/usr/local/lib/lightsail-vpn/traffic-update.py',*sys.argv[1:]])
