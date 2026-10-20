#!/usr/bin/env bash
# 从基础旧安装补齐组件；不同于 update.sh 的纯代码更新。
set -euo pipefail
src="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$src/scripts/migrate.py" "$@"
