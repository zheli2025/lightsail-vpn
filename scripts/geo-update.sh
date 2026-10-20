#!/usr/bin/env bash
#
# geo-update - 更新 Clash GEO 数据和 CN-only MRS 规则集并原子发布到 Nginx Web 目录
#
# 用法:
#   sudo geo-update update   立即更新（默认）
#   sudo geo-update status   查看数据库、状态文件与定时任务
#
# 设计要点:
#   - 数据源: MetaCubeX/meta-rules-dat（DAT/MMDB 用 release，MRS 用 meta 分支）
#   - 校验: DAT/MMDB 优先比对官方 .sha256sum；MRS 做大小/Zstandard 格式检查
#   - 发布: 临时文件 + 原子 rename，旧文件在替换前始终可用
#   - 并发: flock 防止 timer 与手动更新同时执行
#   - 与 sub update 完全异步: 两者写不同文件、互不调用
#
set -euo pipefail

GEO_DIR="${GEO_DIR:-/var/www/html/geo}"
STATE_DIR="${STATE_DIR:-/var/lib/geo-update}"
STATE_FILE="${STATE_DIR}/state"
LOG_FILE="${LOG_FILE:-/var/log/geo-update.log}"
LOCK_DIR="${LOCK_DIR:-/run/geo-update}"
LOCK_FILE="${LOCK_DIR}/lock"

GEO_REPO="${GEO_REPO:-MetaCubeX/meta-rules-dat}"
GEO_BRANCH="${GEO_BRANCH:-release}"
GEO_TAG="${GEO_TAG:-latest}"

DATABASE_FILES=(geoip.dat geosite.dat country.mmdb)
MRS_FILES=(geosite-cn.mrs geoip-cn.mrs)
FILES=("${DATABASE_FILES[@]}" "${MRS_FILES[@]}")

declare -A MIN_SIZE=(
  [geoip.dat]=5242880
  [geosite.dat]=1048576
  [country.mmdb]=2097152
  [geosite-cn.mrs]=262144
  [geoip-cn.mrs]=16384
)

declare -A MRS_PATH=(
  [geosite-cn.mrs]=geo/geosite/cn.mrs
  [geoip-cn.mrs]=geo/geoip/cn.mrs
)

SOURCES=(
  "https://github.com/${GEO_REPO}/releases/download/${GEO_TAG}"
  "https://cdn.jsdelivr.net/gh/${GEO_REPO}@${GEO_BRANCH}"
  "https://testingcf.jsdelivr.net/gh/${GEO_REPO}@${GEO_BRANCH}"
)

MRS_SOURCES=(
  "https://raw.githubusercontent.com/${GEO_REPO}/meta"
  "https://cdn.jsdelivr.net/gh/${GEO_REPO}@meta"
  "https://testingcf.jsdelivr.net/gh/${GEO_REPO}@meta"
)

for _cmd in curl find flock sha256sum stat tail head od tr; do
  command -v "$_cmd" >/dev/null 2>&1 || { echo "error: 缺少命令: $_cmd" >&2; exit 1; }
done

log() {
  local line
  line="$(date --iso-8601=seconds) $*"
  printf '%s\n' "$line" >> "$LOG_FILE" 2>/dev/null || true
  printf '%s\n' "$line"
}

rotate_log() {
  [[ -f "$LOG_FILE" ]] || return 0
  local size
  size="$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)"
  if (( size > 1048576 )); then
    mv -f "$LOG_FILE" "${LOG_FILE}.1" 2>/dev/null || true
    : > "$LOG_FILE" 2>/dev/null || true
  fi
}

acquire_lock() {
  mkdir -p "$LOCK_DIR" 2>/dev/null || true
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "geo-update: 已有更新任务在运行，跳过本次"
    exit 0
  fi
}

verify_fallback() {
  local name="$1" file="$2" size="$3" min
  min="${MIN_SIZE[$name]:-1048576}"
  (( size >= min )) || return 1
  if [[ "$name" == "country.mmdb" ]]; then
    # MaxMind DB 的魔数位于文件尾部元数据区
    tail -c 512 "$file" | LC_ALL=C grep -aFq $'\xab\xcd\xefMaxMind.com' || return 1
  elif [[ "$name" == *.mrs ]]; then
    # MetaCubeX 的 MRS 发布物为 Zstandard 压缩格式，先拒绝 HTML/截断文件。
    [[ "$(head -c 4 "$file" | od -An -tx1 | tr -d '[:space:]')" == "28b52ffd" ]] || return 1
  elif LC_ALL=C grep -aqi '<html' "$file"; then
    return 1
  fi
  return 0
}

fetch_mrs_one() {
  local name="$1" tmp="$2" base_url actual actual_size path
  path="${MRS_PATH[$name]}"
  for base_url in "${MRS_SOURCES[@]}"; do
    if curl -fsSL --retry 3 --retry-delay 5 --retry-all-errors \
         --connect-timeout 10 --max-time 600 -o "$tmp" "${base_url}/${path}" 2>/dev/null; then
      actual_size="$(stat -c %s "$tmp")"
      if verify_fallback "$name" "$tmp" "$actual_size"; then
        actual="$(sha256sum "$tmp" | awk '{print $1}')"
        printf '%s\n' "$actual"
        return 0
      fi
      log "WARN ${name}: ${base_url} 下载内容未通过大小/格式检查（size=${actual_size}）" >&2
    fi
    rm -f "$tmp"
  done
  return 1
}

fetch_one() {
  local name="$1" tmp="$2"
  local base_url="" sha="" sum_tmp actual actual_size
  for base_url in "${SOURCES[@]}"; do
    sha=""
    sum_tmp="${tmp}.sha256sum"
    if curl -fsSL --connect-timeout 10 --max-time 60 -o "$sum_tmp" \
         "${base_url}/${name}.sha256sum" 2>/dev/null; then
      sha="$(awk 'NR==1 {print $1}' "$sum_tmp" | tr 'A-F' 'a-f')"
      [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || sha=""
    fi
    rm -f "$sum_tmp"

    if curl -fsSL --retry 3 --retry-delay 5 --retry-all-errors \
         --connect-timeout 10 --max-time 600 -o "$tmp" "${base_url}/${name}" 2>/dev/null; then
      actual_size="$(stat -c %s "$tmp")"
      actual="$(sha256sum "$tmp" | awk '{print $1}')"
      if [[ -n "$sha" ]]; then
        if [[ "$actual" == "$sha" ]]; then
          printf '%s\n' "$actual"
          return 0
        fi
        log "WARN ${name}: ${base_url} SHA256 不匹配（期望 ${sha}，实际 ${actual}）" >&2
      elif verify_fallback "$name" "$tmp" "$actual_size"; then
        log "WARN ${name}: ${base_url} 无有效校验文件，已通过大小/格式检查" >&2
        printf '%s\n' "$actual"
        return 0
      else
        log "WARN ${name}: ${base_url} 下载内容未通过完整性检查（size=${actual_size}）" >&2
      fi
    fi
    rm -f "$tmp"
  done
  return 1
}

update_one() {
  local name="$1"
  local tmp="${GEO_DIR}/.tmp/${name}.$$" final="${GEO_DIR}/${name}" sha cur
  mkdir -p "${GEO_DIR}/.tmp"
  if [[ -n "${MRS_PATH[$name]:-}" ]]; then
    sha="$(fetch_mrs_one "$name" "$tmp")" || sha=""
  else
    sha="$(fetch_one "$name" "$tmp")" || sha=""
  fi
  if [[ -z "$sha" ]]; then
    log "ERROR ${name}: 所有源均失败，保留现有文件"
    return 1
  fi
  if [[ -f "$final" ]]; then
    cur="$(sha256sum "$final" | awk '{print $1}')"
    if [[ "$cur" == "$sha" ]]; then
      log "OK ${name}: 无变化（${sha}）"
      rm -f "$tmp"
      return 0
    fi
  fi
  mv -f "$tmp" "$final"
  chown root:root "$final" 2>/dev/null || true
  chmod 644 "$final"
  log "OK ${name}: 已更新（sha256 ${sha}，size $(stat -c %s "$final")）"
}

write_state() {
  local tmp="${STATE_FILE}.tmp.$$" name
  mkdir -p "$STATE_DIR"
  {
    printf 'updated_at=%s\n' "$(date --iso-8601=seconds)"
    for name in "${FILES[@]}"; do
      if [[ -f "${GEO_DIR}/${name}" ]]; then
        printf '%s.sha256=%s\n' "$name" "$(sha256sum "${GEO_DIR}/${name}" | awk '{print $1}')"
        printf '%s.bytes=%s\n' "$name" "$(stat -c %s "${GEO_DIR}/${name}")"
      fi
    done
  } > "$tmp"
  mv -f "$tmp" "$STATE_FILE"
  chmod 600 "$STATE_FILE" 2>/dev/null || true
}

cmd_update() {
  acquire_lock
  rotate_log
  find "${GEO_DIR}/.tmp" -mindepth 1 -maxdepth 1 -type f -mmin +120 -delete 2>/dev/null || true
  log "== 开始 GEO 数据与 CN MRS 规则集更新 =="
  local failed=0 name
  for name in "${FILES[@]}"; do
    update_one "$name" || failed=1
  done
  write_state
  if (( failed )); then
    log "== 更新完成，存在失败项（下次定时任务会自动重试） =="
    return 1
  fi
  log "== 更新完成 =="
}

cmd_status() {
  local name f
  echo "GEO 目录: ${GEO_DIR}"
  echo "状态文件: ${STATE_FILE}"
  echo "GEO 数据源: ${SOURCES[0]}（主） / jsDelivr（备）"
  echo "CN MRS 源: ${MRS_SOURCES[0]}（主） / jsDelivr（备）"
  echo
  for name in "${FILES[@]}"; do
    f="${GEO_DIR}/${name}"
    if [[ -f "$f" ]]; then
      printf '%-16s 存在  size=%-10s sha256=%s\n' "$name" "$(stat -c %s "$f")" "$(sha256sum "$f" | awk '{print $1}')"
    else
      printf '%-16s 缺失\n' "$name"
    fi
  done
  echo
  if [[ -f "$STATE_FILE" ]]; then
    echo "最近更新: $(sed -n 's/^updated_at=//p' "$STATE_FILE")"
  else
    echo "最近更新: 从未成功（状态文件不存在）"
  fi
  echo
  if systemctl list-timers geo-update.timer --no-pager >/dev/null 2>&1; then
    systemctl list-timers geo-update.timer --no-pager
  else
    echo "定时器 geo-update.timer: 未安装"
  fi
  echo
  echo "最近日志（${LOG_FILE}）:"
  tail -n 10 "$LOG_FILE" 2>/dev/null || echo "（无日志）"
}

action="${1:-update}"
case "$action" in
  update) cmd_update ;;
  status) cmd_status ;;
  *) echo "用法: geo-update update|status" >&2; exit 2 ;;
esac
