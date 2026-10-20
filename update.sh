#!/usr/bin/env bash
# 已安装本项目服务时使用；默认只读检查，显式 --apply 才更新。
set -euo pipefail
src="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$src/scripts/update.py" "$@"
