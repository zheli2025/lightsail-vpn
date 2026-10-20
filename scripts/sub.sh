#!/usr/bin/env bash
#
# sub - Clash 订阅文件管理工具
#
# 用法:
#   sudo sub update           刷新订阅 YAML（内容同步自 Hysteria 配置）
#   sudo sub name [name]      查看或修改订阅名字
#   sudo sub node [name]      查看或修改节点名称
#   sudo sub token            生成/轮换订阅密钥并更新 Nginx 鉴权
#   sudo sub rotate           轮换 Hysteria 认证/混淆密码并同步订阅
#   sudo sub ip <新IP>         切换公网 IP 的证书、订阅和流量统计配置
#   sudo sub url              打印当前订阅 URL（含密钥）
#   sudo sub status           查看当前订阅状态
#   sudo sub usage [period]   查看按域名统计的上传/下载（today/month/all）
#   sudo sub usage tui         交互式终端界面（排序/搜索/滚动）
#   sudo sub help             显示帮助
#
set -euo pipefail

HYSTERIA_CONFIG="${HYSTERIA_CONFIG:-/etc/hysteria/config.yaml}"
CERT_FILE="${CERT_FILE:-/etc/hysteria/certs/server.crt}"
HYSTERIA_CERT_DIR="${HYSTERIA_CERT_DIR:-$(dirname "$CERT_FILE")}"
HYSTERIA_KEY_FILE="${HYSTERIA_KEY_FILE:-${HYSTERIA_CERT_DIR}/server.key}"
WEBROOT="${WEBROOT:-/var/www/html}"
GEO_DIR="${GEO_DIR:-${WEBROOT}/geo}"
STATE_FILE="${STATE_FILE:-/etc/hysteria/.sub-state}"
NODE_FILE="${NODE_FILE:-/etc/hysteria/.sub-node}"
TOKEN_FILE="${TOKEN_FILE:-/etc/hysteria/.sub-token}"
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-available/vpn-subscription}"
NGINX_ENABLED="${NGINX_ENABLED:-/etc/nginx/sites-enabled/vpn-subscription}"
NGINX_DEFAULT="${NGINX_DEFAULT:-/etc/nginx/sites-available/default}"
NGINX_DEFAULT_ENABLED="${NGINX_DEFAULT_ENABLED:-/etc/nginx/sites-enabled/default}"
BACKUP_DIR="${BACKUP_DIR:-/etc/nginx/.sub-backup}"
DEFAULT_NAME="${DEFAULT_NAME:-aws-hysteria}"
DEFAULT_NODE="${DEFAULT_NODE:-AWS-HY2}"
PORT="${PORT:-443}"
GEO_UPDATE_BIN="${GEO_UPDATE_BIN:-/usr/local/bin/geo-update}"
TRAFFIC_UPDATE_BIN="${TRAFFIC_UPDATE_BIN:-/usr/local/bin/traffic-update}"
TRAFFIC_NGINX_SNIPPET="${TRAFFIC_NGINX_SNIPPET:-/etc/nginx/subscription-userinfo.conf}"
TRAFFIC_STATE_FILE="${TRAFFIC_STATE_FILE:-/var/lib/traffic/state.json}"
DOMAIN_USAGE_BIN="${DOMAIN_USAGE_BIN:-/usr/local/bin/domain-usage}"
TLS_CERT_FILE_OVERRIDE="${TLS_CERT_FILE:-}"
TLS_KEY_FILE_OVERRIDE="${TLS_KEY_FILE:-}"
GEO_BASE_URL_OVERRIDE="${GEO_BASE_URL:-}"
CERTBOT_BIN="${CERTBOT_BIN:-/snap/bin/certbot}"
LETSENCRYPT_DIR="${LETSENCRYPT_DIR:-/etc/letsencrypt}"
TRAFFIC_SERVICE_OVERRIDE="${TRAFFIC_SERVICE_OVERRIDE:-/etc/systemd/system/traffic-update.service.d/migrate.conf}"

die() {
  echo "error: $*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die "请用 sudo 运行：sudo sub <command>"

get_public_ip() {
  openssl x509 -in "$CERT_FILE" -noout -ext subjectAltName 2>/dev/null \
    | grep -oP 'IP Address:\K[0-9.]+' \
    | head -n 1 || true
}

subscription_scheme() {
  if [[ -r "$TLS_CERT_FILE" && -r "$TLS_KEY_FILE" ]]; then
    printf '%s' https
  else
    printf '%s' http
  fi
}

set_public_ip() {
  PUBLIC_IP="$1"
  TLS_CERT_FILE="${TLS_CERT_FILE_OVERRIDE:-${LETSENCRYPT_DIR}/live/${PUBLIC_IP}/fullchain.pem}"
  TLS_KEY_FILE="${TLS_KEY_FILE_OVERRIDE:-${LETSENCRYPT_DIR}/live/${PUBLIC_IP}/privkey.pem}"
  SUBSCRIPTION_SCHEME="$(subscription_scheme)"
  GEO_BASE_URL="${GEO_BASE_URL_OVERRIDE:-${SUBSCRIPTION_SCHEME}://${PUBLIC_IP}/geo}"
}

PUBLIC_IP="$(get_public_ip)"
PUBLIC_IP="${PUBLIC_IP:-43.200.18.124}"
set_public_ip "$PUBLIC_IP"

read_secret() {
  local section="$1"
  case "$section" in
    auth)
      sed -n '/^auth:/,/^obfs:/p' "$HYSTERIA_CONFIG" \
        | grep 'password:' | head -n 1 \
        | sed -E 's/^[[:space:]]*password:[[:space:]]*"([^"]*)"[[:space:]]*$/\1/' || true
      ;;
    obfs)
      sed -n '/^obfs:/,$p' "$HYSTERIA_CONFIG" \
        | grep 'password:' | head -n 1 \
        | sed -E 's/^[[:space:]]*password:[[:space:]]*"([^"]*)"[[:space:]]*$/\1/' || true
      ;;
  esac
}

sanitize_name() {
  local name="$1"
  name="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9-]+/-/g; s/^-+|-+$//g')"
  [[ -n "$name" ]] || die "订阅名字不能为空"
  [[ "${#name}" -le 32 ]] || die "订阅名字太长（最多 32 个字符）"
  [[ "$name" =~ ^[a-z0-9-]+$ ]] || die "订阅名字只能包含字母、数字和连字符"
  printf '%s' "$name"
}

sanitize_node() {
  local node="$1"
  node="$(printf '%s' "$node" | tr -d '"' | tr -d '\\' \
    | sed -E 's/[[:cntrl:]]//g; s/^[[:space:]]+//; s/[[:space:]]+$//')"
  [[ -n "$node" ]] || die "节点名称不能为空"
  [[ "${#node}" -le 32 ]] || die "节点名称太长（最多 32 个字符）"
  printf '%s' "$node"
}

current_name() {
  if [[ -f "$STATE_FILE" ]]; then
    basename "$(cat "$STATE_FILE")" .yaml
  else
    printf '%s' "$DEFAULT_NAME"
  fi
}

current_node() {
  if [[ -s "$NODE_FILE" ]]; then
    cat "$NODE_FILE"
  else
    printf '%s' "$DEFAULT_NODE"
  fi
}

get_or_create_token() {
  if [[ -s "$TOKEN_FILE" ]]; then
    cat "$TOKEN_FILE"
  else
    generate_token
  fi
}

generate_token() {
  local token token_tmp
  token="$(openssl rand -hex 32)"
  token_tmp="$(mktemp "${TOKEN_FILE}.tmp.XXXXXX")"
  printf '%s\n' "$token" > "$token_tmp"
  chmod 600 "$token_tmp"
  mv -f "$token_tmp" "$TOKEN_FILE"
  printf '%s' "$token"
}

ensure_nginx_site() {
  local name="$1" token="$2"
  local site_tmp site_backup="" enabled_target="" enabled_exists=0 default_enabled=0 tls_enabled=0
  if [[ ! -f "$TRAFFIC_NGINX_SNIPPET" ]]; then
    : > "$TRAFFIC_NGINX_SNIPPET"
    chmod 644 "$TRAFFIC_NGINX_SNIPPET"
  fi
  mkdir -p "$BACKUP_DIR"
  if [[ -e "$NGINX_DEFAULT" && ! -e "$BACKUP_DIR/default" ]]; then
    cp -a "$NGINX_DEFAULT" "$BACKUP_DIR/default"
  fi

  # 先用候选配置完成 nginx -t；失败时不改动当前订阅站点或已运行的 Nginx。
  site_tmp="$(mktemp "${NGINX_SITE}.tmp.XXXXXX")"
  if [[ -e "$NGINX_SITE" ]]; then
    site_backup="$(mktemp "${NGINX_SITE}.bak.XXXXXX")"
    cp -a "$NGINX_SITE" "$site_backup"
  fi
  if [[ -e "$NGINX_ENABLED" || -L "$NGINX_ENABLED" ]]; then
    enabled_target="$(readlink "$NGINX_ENABLED" || true)"
    enabled_exists=1
  fi
  if [[ -e "$NGINX_DEFAULT_ENABLED" || -L "$NGINX_DEFAULT_ENABLED" ]]; then
    default_enabled=1
  fi

  if [[ -r "$TLS_CERT_FILE" && -r "$TLS_KEY_FILE" ]]; then
    tls_enabled=1
  fi

  nginx_locations() {
    cat <<EOF
    root ${WEBROOT};
    index index.html index.htm index.nginx-debian.html;

    location ^~ /.well-known/acme-challenge/ {
        root ${WEBROOT};
        default_type text/plain;
        access_log off;
    }

    location /geo/ {
        alias ${WEBROOT}/geo/;
        default_type application/octet-stream;
        expires 1h;
        access_log off;
    }

    location = /${name}.yaml {
        default_type text/plain;
        include ${TRAFFIC_NGINX_SNIPPET};
        if (\$arg_token != "${token}") {
            return 403;
        }
        access_log off;
        limit_req zone=sub_limit burst=5 nodelay;
    }

    location / {
        try_files \$uri \$uri/ =404;
    }
EOF
  }

  {
  cat <<EOF
limit_req_zone \$binary_remote_addr zone=sub_limit:10m rate=10r/m;

server {
    listen 80;
    listen [::]:80;
    server_name _;
EOF
  nginx_locations
  cat <<EOF
}
EOF
  if [[ "$tls_enabled" -eq 1 ]]; then
    cat <<EOF

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name ${PUBLIC_IP};

    ssl_certificate ${TLS_CERT_FILE};
    ssl_certificate_key ${TLS_KEY_FILE};
EOF
    nginx_locations
    cat <<EOF
}
EOF
  fi
  } > "$site_tmp"

  chmod 644 "$site_tmp"
  ln -sfn "$site_tmp" "$NGINX_ENABLED"
  rm -f "$NGINX_DEFAULT_ENABLED"

  if ! nginx -t >/dev/null 2>&1; then
    echo "error: nginx 配置测试失败，保留原订阅配置" >&2
    if [[ "$enabled_exists" -eq 1 ]]; then
      ln -sfn "$enabled_target" "$NGINX_ENABLED"
    else
      rm -f "$NGINX_ENABLED"
    fi
    if [[ "$default_enabled" -eq 1 ]]; then
      ln -sfn "$NGINX_DEFAULT" "$NGINX_DEFAULT_ENABLED"
    fi
    rm -f "$site_tmp" "$site_backup"
    return 1
  fi

  mv -f "$site_tmp" "$NGINX_SITE"
  ln -sfn "$NGINX_SITE" "$NGINX_ENABLED"

  if ! systemctl reload nginx >/dev/null; then
    echo "error: Nginx 重载失败，正在恢复原订阅配置" >&2
    if [[ -n "$site_backup" ]]; then
      mv -f "$site_backup" "$NGINX_SITE"
    else
      rm -f "$NGINX_SITE"
    fi
    if [[ "$enabled_exists" -eq 1 ]]; then
      ln -sfn "$enabled_target" "$NGINX_ENABLED"
    else
      rm -f "$NGINX_ENABLED"
    fi
    if [[ "$default_enabled" -eq 1 ]]; then
      ln -sfn "$NGINX_DEFAULT" "$NGINX_DEFAULT_ENABLED"
    fi
    nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1 || true
    return 1
  fi

  rm -f "$site_backup"
}

cmd_update() {
  if [[ $# -gt 0 ]]; then
    die "update 不接受名字参数，改名请用: sudo sub name <name>"
  fi

  local name node_name file old_file=""
  local auth_password obfs_password token geo_missing=0 f

  name="$(current_name)"
  node_name="$(current_node)"
  file="$WEBROOT/$name.yaml"

  [[ -f "$HYSTERIA_CONFIG" ]] || die "找不到 Hysteria 配置：$HYSTERIA_CONFIG"
  auth_password="$(read_secret auth)"
  obfs_password="$(read_secret obfs)"
  [[ -n "$auth_password" ]] || die "无法从 $HYSTERIA_CONFIG 读取 auth 密码"
  [[ -n "$obfs_password" ]] || die "无法从 $HYSTERIA_CONFIG 读取 obfs 密码"

  for f in geosite-cn.mrs geoip-cn.mrs; do
    if [[ ! -f "$GEO_DIR/$f" ]]; then
      echo "warning: CN MRS 规则集缺失: $GEO_DIR/$f（运行 sudo sub geo update）" >&2
      geo_missing=1
    fi
  done

  if [[ -f "$STATE_FILE" ]]; then
    old_file="$(cat "$STATE_FILE")"
  fi

  cat > "$file.tmp" <<EOF
mixed-port: 7890
allow-lan: false
mode: rule
log-level: info
ipv6: false

proxies:
  - name: "${node_name}"
    type: hysteria2
    server: ${PUBLIC_IP}
    port: ${PORT}
    password: "${auth_password}"
    obfs: salamander
    obfs-password: "${obfs_password}"
    skip-cert-verify: true

rule-providers:
  cn-domain:
    type: http
    behavior: domain
    format: mrs
    url: "${GEO_BASE_URL}/geosite-cn.mrs"
    path: ./rule-set/geosite-cn.mrs
    interval: 86400
  cn-ip:
    type: http
    behavior: ipcidr
    format: mrs
    url: "${GEO_BASE_URL}/geoip-cn.mrs"
    path: ./rule-set/geoip-cn.mrs
    interval: 86400

proxy-groups:
  - name: PROXY
    type: select
    proxies:
      - "${node_name}"
      - DIRECT

rules:
  - IP-CIDR,127.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
  - IP-CIDR,100.64.0.0/10,DIRECT,no-resolve
  - IP-CIDR6,::1/128,DIRECT,no-resolve
  - IP-CIDR6,fc00::/7,DIRECT,no-resolve
  - IP-CIDR6,fe80::/10,DIRECT,no-resolve
  - RULE-SET,cn-domain,DIRECT
  - RULE-SET,cn-ip,DIRECT,no-resolve
  - MATCH,PROXY
EOF

  mv "$file.tmp" "$file"
  chown root:root "$file"
  chmod 644 "$file"
  printf '%s\n' "$file" > "$STATE_FILE"
  chmod 600 "$STATE_FILE"

  if [[ -n "$old_file" && "$old_file" != "$file" && -f "$old_file" ]]; then
    echo "已移除旧订阅文件: $old_file"
    rm -f "$old_file"
  fi

  token="$(get_or_create_token)"
  ensure_nginx_site "$name" "$token"

  echo "订阅已更新: $file"
  echo "订阅 URL: ${SUBSCRIPTION_SCHEME}://${PUBLIC_IP}/${name}.yaml?token=${token}"
  if [[ "$geo_missing" -eq 1 ]]; then
    echo "提示: CN MRS 规则集尚未就绪，先运行 sudo sub geo update"
  fi
}

cmd_name() {
  local new_name old_name old_file new_file token old_state="" had_state=0

  if [[ $# -eq 0 ]]; then
    echo "当前订阅名字: $(current_name)"
    return
  fi
  [[ $# -eq 1 ]] || die "name 只接受一个订阅名字"

  new_name="$(sanitize_name "$1")"
  old_name="$(current_name)"
  if [[ "$new_name" == "$old_name" ]]; then
    echo "订阅名字未变化: ${old_name}"
    return
  fi

  old_file="$WEBROOT/$old_name.yaml"
  new_file="$WEBROOT/$new_name.yaml"

  if [[ -e "$new_file" ]]; then
    die "目标订阅文件已存在: $new_file"
  fi

  # 允许在首次生成订阅前先设定名字；此时不发布一个指向不存在文件的 Nginx 路由。
  if [[ ! -f "$old_file" ]]; then
    printf '%s\n' "$new_file" > "$STATE_FILE"
    chmod 600 "$STATE_FILE"
    echo "订阅名字已设为: ${new_name}（尚未生成订阅，运行 sudo sub update 生效）"
    return
  fi

  if [[ -f "$STATE_FILE" ]]; then
    old_state="$(cat "$STATE_FILE")"
    had_state=1
  fi
  mv "$old_file" "$new_file"
  chown root:root "$new_file"
  chmod 644 "$new_file"
  printf '%s\n' "$new_file" > "$STATE_FILE"
  chmod 600 "$STATE_FILE"

  token="$(get_or_create_token)"
  if ! ensure_nginx_site "$new_name" "$token"; then
    [[ -f "$new_file" ]] && mv "$new_file" "$old_file"
    if [[ "$had_state" -eq 1 ]]; then
      printf '%s\n' "$old_state" > "$STATE_FILE"
      chmod 600 "$STATE_FILE"
    else
      rm -f "$STATE_FILE"
    fi
    return 1
  fi

  echo "订阅名字已改为: ${new_name}"
  echo "订阅 URL: ${SUBSCRIPTION_SCHEME}://${PUBLIC_IP}/${new_name}.yaml?token=${token}"
}

cmd_node() {
  local node

  if [[ $# -eq 0 ]]; then
    echo "当前节点名称: $(current_node)"
    return
  fi
  [[ $# -eq 1 ]] || die "node 只接受一个节点名称"

  node="$(sanitize_node "$1")"
  printf '%s\n' "$node" > "$NODE_FILE"
  chmod 600 "$NODE_FILE"

  echo "节点名称已改为: ${node}"
  cmd_update
}

cmd_token() {
  local name token token_tmp
  [[ $# -eq 0 ]] || die "token 不接受参数"
  name="$(current_name)"
  [[ -f "$WEBROOT/$name.yaml" ]] || die "订阅文件不存在，先运行：sudo sub update"
  # token 命令的语义是轮换，而不是读取现有 token。
  token="$(openssl rand -hex 32)"
  ensure_nginx_site "$name" "$token"
  token_tmp="$(mktemp "${TOKEN_FILE}.tmp.XXXXXX")"
  printf '%s\n' "$token" > "$token_tmp"
  chmod 600 "$token_tmp"
  mv -f "$token_tmp" "$TOKEN_FILE"
  echo "订阅密钥已更新"
  echo "订阅 URL: ${SUBSCRIPTION_SCHEME}://${PUBLIC_IP}/${name}.yaml?token=${token}"
}

cmd_geo() {
  local action="${1:-status}"
  [[ -x "$GEO_UPDATE_BIN" ]] || die "找不到 GEO 更新脚本: $GEO_UPDATE_BIN"
  [[ $# -le 1 ]] || die "geo 用法: sudo sub geo update|status"
  case "$action" in
    update|status) exec "$GEO_UPDATE_BIN" "$action" ;;
    *) die "geo 用法: sudo sub geo update|status" ;;
  esac
}

cmd_traffic() {
  local action="${1:-status}"
  [[ -x "$TRAFFIC_UPDATE_BIN" ]] || die "找不到流量更新脚本: $TRAFFIC_UPDATE_BIN"
  [[ $# -le 1 ]] || die "traffic 用法: sudo sub traffic update|status"
  case "$action" in
    update|status) exec "$TRAFFIC_UPDATE_BIN" "$action" ;;
    *) die "traffic 用法: sudo sub traffic update|status" ;;
  esac
}

cmd_usage() {
  local action="${1:-today}"
  [[ -x "$DOMAIN_USAGE_BIN" ]] || die "找不到域名流量统计脚本: $DOMAIN_USAGE_BIN"
  case "$action" in
    today|month|all)
      exec "$DOMAIN_USAGE_BIN" report "$action" "${@:2}" ;;
    tui)
      exec "$DOMAIN_USAGE_BIN" tui ;;
    status|prune)
      exec "$DOMAIN_USAGE_BIN" "$action" "${@:2}" ;;
    *) die "usage 用法: sudo sub usage [today|month|all|status|tui] [--json]" ;;
  esac
}

cmd_rotate() {
  local new_auth new_obfs backup

  [[ $# -eq 0 ]] || die "rotate 不接受参数"

  [[ -f "$HYSTERIA_CONFIG" ]] || die "找不到 Hysteria 配置：$HYSTERIA_CONFIG"
  [[ -n "$(read_secret auth)" ]] || die "无法从 $HYSTERIA_CONFIG 读取 auth 密码"
  [[ -n "$(read_secret obfs)" ]] || die "无法从 $HYSTERIA_CONFIG 读取 obfs 密码"

  new_auth="$(openssl rand -hex 16)"
  new_obfs="$(openssl rand -hex 16)"
  backup="${HYSTERIA_CONFIG}.bak-$(date +%Y%m%d%H%M%S)"
  cp -a "$HYSTERIA_CONFIG" "$backup"

  awk -v new_auth="$new_auth" -v new_obfs="$new_obfs" '
    BEGIN { in_auth = 0; in_obfs = 0 }
    /^auth:/ { in_auth = 1; in_obfs = 0; print; next }
    /^obfs:/ { in_auth = 0; in_obfs = 1; print; next }
    /^[a-z]/ { in_auth = 0; in_obfs = 0 }
    in_auth && /^[[:space:]]*password:/ { sub(/"[^"]*"/, "\"" new_auth "\""); in_auth = 0; print; next }
    in_obfs && /^[[:space:]]*password:/ { sub(/"[^"]*"/, "\"" new_obfs "\""); in_obfs = 0; print; next }
    { print }
  ' "$HYSTERIA_CONFIG" > "$HYSTERIA_CONFIG.tmp"

  mv "$HYSTERIA_CONFIG.tmp" "$HYSTERIA_CONFIG"
  chown root:root "$HYSTERIA_CONFIG"
  chmod 644 "$HYSTERIA_CONFIG"

  if ! systemctl restart hysteria-server.service >/dev/null 2>&1 \
     || [[ "$(systemctl is-active hysteria-server.service)" != "active" ]]; then
    echo "error: Hysteria 重启失败，正在恢复原配置" >&2
    mv -f "$backup" "$HYSTERIA_CONFIG"
    systemctl restart hysteria-server.service >/dev/null 2>&1 || true
    return 1
  fi

  echo "Hysteria 密码已轮换，旧密码已失效"
  echo "配置备份: $backup"
  cmd_update
}

valid_ipv4() {
  local ip="$1" octet
  local -a octets
  IFS='.' read -r -a octets <<< "$ip"
  [[ "${#octets[@]}" -eq 4 ]] || return 1
  for octet in "${octets[@]}"; do
    [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
    (( 10#$octet <= 255 )) || return 1
  done
}

update_traffic_public_ip() {
  local new_ip="$1" override_tmp
  [[ -f "$TRAFFIC_SERVICE_OVERRIDE" ]] || return 0
  override_tmp="$(mktemp "${TRAFFIC_SERVICE_OVERRIDE}.tmp.XXXXXX")"
  sed -E "s|^Environment=LS_PUBLIC_IP=.*$|Environment=LS_PUBLIC_IP=${new_ip}|" \
    "$TRAFFIC_SERVICE_OVERRIDE" > "$override_tmp"
  if ! grep -qx "Environment=LS_PUBLIC_IP=${new_ip}" "$override_tmp"; then
    rm -f "$override_tmp"
    die "无法更新流量统计的 LS_PUBLIC_IP：$TRAFFIC_SERVICE_OVERRIDE"
  fi
  mv -f "$override_tmp" "$TRAFFIC_SERVICE_OVERRIDE"
  systemctl daemon-reload
}

cmd_ip() {
  local new_ip old_ip="$PUBLIC_IP" stamp backup_dir tmp_dir

  [[ $# -eq 1 ]] || die "ip 用法: sudo sub ip <新的公网 IPv4>"
  new_ip="$1"
  valid_ipv4 "$new_ip" || die "公网 IPv4 格式不正确: $new_ip"
  [[ "$new_ip" != "$old_ip" ]] || die "新 IP 与当前 IP 相同: $new_ip"
  [[ -x "$CERTBOT_BIN" ]] || die "找不到 Certbot: $CERTBOT_BIN"
  [[ -d "$WEBROOT" ]] || die "订阅目录不存在: $WEBROOT"
  [[ -f "$CERT_FILE" ]] || die "找不到 Hysteria 证书: $CERT_FILE"

  echo "== 1/4 为新 IP 申请 Let's Encrypt 证书 =="
  echo "提示: 新静态 IP 必须已 attach 到本实例，且 Lightsail 已放行 TCP 80/443。"
  "$CERTBOT_BIN" certonly \
    --non-interactive \
    --agree-tos \
    --preferred-profile shortlived \
    --webroot \
    --webroot-path "$WEBROOT" \
    --ip-address "$new_ip"
  [[ -r "${LETSENCRYPT_DIR}/live/${new_ip}/fullchain.pem" \
     && -r "${LETSENCRYPT_DIR}/live/${new_ip}/privkey.pem" ]] \
    || die "新 IP 证书签发后不可读: $new_ip"

  echo "== 2/4 更新 Hysteria 自签证书并重启服务 =="
  stamp="$(date +%Y%m%d%H%M%S)"
  backup_dir="${HYSTERIA_CERT_DIR}/.ip-change-${stamp}"
  tmp_dir="$(mktemp -d)"
  install -d -m 0700 "$backup_dir"
  cp -a "$CERT_FILE" "$HYSTERIA_KEY_FILE" "$backup_dir/"
  if ! openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
       -keyout "$tmp_dir/server.key" \
       -out "$tmp_dir/server.crt" \
       -days 3650 \
       -subj "/CN=${new_ip}" \
       -addext "subjectAltName=IP:${new_ip}"; then
    rm -rf "$tmp_dir"
    die "生成 Hysteria 自签证书失败"
  fi
  install -o hysteria -g hysteria -m 0600 "$tmp_dir/server.key" "$HYSTERIA_KEY_FILE"
  install -o hysteria -g hysteria -m 0644 "$tmp_dir/server.crt" "$CERT_FILE"
  rm -rf "$tmp_dir"
  if ! systemctl restart hysteria-server.service >/dev/null 2>&1 \
     || [[ "$(systemctl is-active hysteria-server.service)" != "active" ]]; then
    echo "error: Hysteria 重启失败，正在恢复旧证书" >&2
    cp -a "$backup_dir/server.crt" "$CERT_FILE"
    cp -a "$backup_dir/server.key" "$HYSTERIA_KEY_FILE"
    chown hysteria:hysteria "$CERT_FILE" "$HYSTERIA_KEY_FILE"
    chmod 0644 "$CERT_FILE"
    chmod 0600 "$HYSTERIA_KEY_FILE"
    systemctl restart hysteria-server.service >/dev/null 2>&1 || true
    return 1
  fi

  echo "== 3/4 更新订阅、HTTPS 和流量统计配置 =="
  set_public_ip "$new_ip"
  update_traffic_public_ip "$new_ip"
  cmd_update

  echo "== 4/4 清理旧 IP 证书 =="
  if [[ -d "${LETSENCRYPT_DIR}/live/${old_ip}" ]]; then
    "$CERTBOT_BIN" delete --non-interactive --cert-name "$old_ip" \
      || echo "warning: 旧 IP 证书未自动删除，可在确认客户端可用后运行: sudo certbot delete --cert-name ${old_ip}" >&2
  fi

  echo "公网 IP 已切换: ${old_ip} -> ${new_ip}"
  echo "Hysteria 证书备份: ${backup_dir}"
  echo "请在客户端重新导入以下订阅 URL："
  cmd_url
}

cmd_url() {
  [[ $# -eq 0 ]] || die "url 不接受参数"
  [[ -s "$TOKEN_FILE" ]] || die "还没有订阅密钥，先运行：sudo sub token"
  local name
  name="$(current_name)"
  echo "${SUBSCRIPTION_SCHEME}://${PUBLIC_IP}/${name}.yaml?token=$(cat "$TOKEN_FILE")"
}

cmd_status() {
  local name
  [[ $# -eq 0 ]] || die "status 不接受参数"
  name="$(current_name)"
  echo "服务器 IP: ${PUBLIC_IP}"
  echo "订阅 HTTPS: $([[ "$SUBSCRIPTION_SCHEME" == https ]] && echo 已启用 || echo 未启用)"
  echo "订阅名字: ${name}"
  echo "节点名称: $(current_node)"
  echo "订阅目录: ${WEBROOT}"
  echo "订阅文件: ${WEBROOT}/${name}.yaml"
  if [[ -f "$WEBROOT/$name.yaml" ]]; then
    echo "文件状态: 存在"
  else
    echo "文件状态: 缺失（运行 sudo sub update 重新生成）"
  fi
  if [[ -s "$TOKEN_FILE" ]]; then
    echo "订阅密钥: 已生成"
  else
    echo "订阅密钥: 未生成（运行 sudo sub token）"
  fi
  if [[ -f "$NGINX_ENABLED" ]]; then
    echo "Nginx 鉴权: 已配置"
  else
    echo "Nginx 鉴权: 未配置（运行 sudo sub token）"
  fi
  if [[ -d "$GEO_DIR" ]]; then
    local geo_ready=0 geo_missing=0 f
    for f in geosite-cn.mrs geoip-cn.mrs; do
      if [[ -f "$GEO_DIR/$f" ]]; then
        geo_ready=$((geo_ready + 1))
      else
        geo_missing=$((geo_missing + 1))
      fi
    done
    if [[ "$geo_missing" -eq 0 ]]; then
      echo "CN MRS 规则集: ${geo_ready}/2 就绪"
    else
      echo "CN MRS 规则集: ${geo_ready}/2 就绪，缺失 ${geo_missing} 个（运行 sudo sub geo update）"
    fi
  else
    echo "CN MRS 规则集: 未安装（运行 sudo sub geo update）"
  fi
  if [[ -s "$TRAFFIC_STATE_FILE" ]]; then
    echo "流量信息: 已获取（sudo sub traffic status 查看）"
  else
    echo "流量信息: 未获取（先为实例角色附加 Lightsail 只读权限，再运行 sudo sub traffic update）"
  fi
}

usage() {
  cat <<EOF
sub - Clash 订阅文件管理工具

用法:
  sudo sub update           刷新订阅 YAML（内容同步自 Hysteria 配置）
  sudo sub name [name]      查看或修改订阅名字（如: sudo sub name my-vpn）
  sudo sub node [name]      查看或修改节点名称（如: sudo sub node My-VPN）
  sudo sub token            生成/轮换订阅密钥并更新 Nginx 鉴权
  sudo sub rotate           轮换 Hysteria 认证/混淆密码并同步订阅
  sudo sub ip <新IP>         自动完成更换静态 IP 后的证书、订阅与流量统计配置
  sudo sub geo update|status 立即更新 / 查看 GEOIP/GeoSite 数据库
  sudo sub traffic update|status 立即获取 / 查看 Lightsail 流量信息
  sudo sub usage [period]   查看按域名统计（today/month/all，默认 today）
  sudo sub usage tui        交互式终端界面（排序/搜索/滚动/自动刷新）
  sudo sub usage status     查看域名流量采集器状态
  sudo sub url              打印当前订阅 URL（含密钥）
  sudo sub status           查看当前订阅状态
  sudo sub help             显示帮助

示例:
  sudo sub update
  sudo sub name my-vpn
  sudo sub node My-VPN
  sudo sub token
  sudo sub rotate
  sudo sub ip 203.0.113.10
  sudo sub geo update
  sudo sub geo status
  sudo sub traffic update
  sudo sub traffic status
  sudo sub usage
  sudo sub usage tui
  sudo sub usage month --json
  sudo sub url
EOF
}

cmd="${1:-help}"
case "$cmd" in
  update) shift; cmd_update "$@" ;;
  name)   shift; cmd_name "$@" ;;
  node)   shift; cmd_node "$@" ;;
  token)  shift; cmd_token "$@" ;;
  rotate) shift; cmd_rotate "$@" ;;
  ip)     shift; cmd_ip "$@" ;;
  geo)    shift; cmd_geo "$@" ;;
  traffic) shift; cmd_traffic "$@" ;;
  usage)   shift; cmd_usage "$@" ;;
  url)    shift; cmd_url "$@" ;;
  status) shift; cmd_status "$@" ;;
  help|-h|--help) usage ;;
  *) die "未知命令: $cmd（运行 sudo sub help 查看用法）" ;;
esac
