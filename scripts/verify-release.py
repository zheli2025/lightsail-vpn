#!/usr/bin/env python3
"""验证官方 hashes.txt 的对应二进制，不执行下载到的程序。"""
import hashlib
from pathlib import Path, PurePosixPath
import sys


def verify(directory, name):
    root=Path(directory)
    entries=[line.split() for line in (root/'hashes.txt').read_text().splitlines()]
    sums=[r[0].lower() for r in entries if len(r)==2 and PurePosixPath(r[1].lstrip('*')).name==name]
    if len(sums)!=1 or hashlib.sha256((root/name).read_bytes()).hexdigest()!=sums[0]:
        raise ValueError('官方 SHA256 校验失败，拒绝安装')


if __name__=='__main__':
    try: verify(*sys.argv[1:])
    except (OSError,ValueError,TypeError) as exc: sys.exit(f'error: {exc}')
