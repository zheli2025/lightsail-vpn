#!/usr/bin/env bash
# 全新 Ubuntu 24.04 / Lightsail 部署入口。不调用 AWS API，不读取 AWS 凭据。
set -euo pipefail
umask 077
usage() {
  echo '用法: sudo bash deploy.sh --ip 公网静态IPv4 --node "节点名称" --email ACME联系邮箱 [--name vpn] [--accept-tos]'
  echo '需事先在 AWS 绑定静态 IP，并开放 TCP 80/443、UDP 443。--accept-tos 表示接受 Let’s Encrypt 服务条款。'
}
die() { echo "error: $*" >&2; exit 1; }
ip='' node='' email='' name='vpn' accept=0
while (($#)); do
  case "$1" in
    --ip|--node|--email|--name)
      (($# >= 2)) || die "$1 缺少值"
      case "$1" in --ip) ip="$2";; --node) node="$2";; --email) email="$2";; --name) name="$2";; esac
      shift 2;;
    --accept-tos) accept=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "未知参数: $1";;
  esac
done
[[ $EUID == 0 ]] || die '请用 sudo bash deploy.sh 运行'
[[ -n "$ip" && -n "$node" && -n "$email" ]] || { usage; exit 2; }
((accept)) || die '请阅读 https://letsencrypt.org/repository/ 后加入 --accept-tos'
# 参数在任何系统修改前校验。
python3 - "$ip" "$node" "$name" "$email" <<'PY'
import ipaddress,re,sys
ip,node,name,email=sys.argv[1:]
try:
    a=ipaddress.IPv4Address(ip)
    assert a.is_global and not a.is_multicast
    assert 1 <= len(node) <= 64 and not any(ord(c)<32 for c in node)
    assert node not in ('DIRECT','REJECT','REJECT-DROP','PASS','COMPATIBLE','PROXY')
    assert re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}',name)
    assert re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email)
except (ValueError,AssertionError):
    sys.exit('error: 检查公网 IPv4、节点名、订阅名及邮箱格式')
PY
source /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || die '当前只支持 Ubuntu 24.04'
src="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec 9>/run/lightsail-vpn-deploy.lock
flock -n 9 || die '另一个部署进程正在运行'
# 拒绝接管原有服务；此脚本在当前生产服务器也会在此停止。
if [[ ! -f /etc/lightsail-vpn/state.json ]]; then
  for path in /etc/hysteria/config.yaml /usr/local/bin/sub /etc/nginx/sites-enabled/vpn-subscription; do
    [[ ! -e "$path" ]] || die "检测到已有服务 $path；请在全新实例上部署"
  done
  if systemctl is-active --quiet nginx || systemctl is-active --quiet hysteria-server; then
    die '已有 Nginx/Hysteria 服务，请使用全新实例'
  fi
fi
echo '1/6 安装系统依赖（不会改变 AWS 或 UFW 防火墙）'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nginx openssl curl ca-certificates python3 python3-yaml snapd zstd
echo '2/6 下载固定版本 Hysteria 并校验官方 SHA256'
case "$(uname -m)" in x86_64) arch=amd64;; aarch64) arch=arm64;; *) die '仅支持 amd64 / arm64';; esac
version='2.13.0'
tmp="$(mktemp -d /tmp/lightsail-vpn-download.XXXXXX)"
trap 'rm -f "$tmp/hysteria-linux-$arch" "$tmp/hashes.txt"; rmdir "$tmp"' EXIT
base="https://github.com/apernet/hysteria/releases/download/app/v${version}"
curl -fSL --retry 3 --connect-timeout 15 --max-time 600 "$base/hysteria-linux-$arch" -o "$tmp/hysteria-linux-$arch"
curl -fSL --retry 3 --max-time 120 "$base/hashes.txt" -o "$tmp/hashes.txt"
python3 - "$tmp" "$arch" <<'PY'
import hashlib,pathlib,sys
root=pathlib.Path(sys.argv[1]); name='hysteria-linux-'+sys.argv[2]
entries=[line.split() for line in (root/'hashes.txt').read_text().splitlines()]
expected=[row[0].lower() for row in entries if len(row)==2 and pathlib.PurePosixPath(row[1].lstrip('*')).name==name]
if len(expected)!=1 or hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected[0]:
    sys.exit('error: 官方 SHA256 校验失败，拒绝安装')
PY
install -m 0755 "$tmp/hysteria-linux-$arch" /usr/local/bin/hysteria
echo '3/6 安装管理脚本、目录和 systemd 单元'
id hysteria >/dev/null 2>&1 || useradd --system --home-dir /var/lib/hysteria --shell /usr/sbin/nologin hysteria
install -d -m 0700 /etc/lightsail-vpn
install -d -m 0750 -o root -g hysteria /etc/hysteria /etc/hysteria/certs
install -d -m 0750 -o root -g www-data /var/lib/lightsail-vpn
install -d -m 0755 /var/lib/lightsail-vpn/geo /var/www/lightsail-vpn /usr/local/lib/lightsail-vpn
install -m 0755 "$src/scripts/sub.py" /usr/local/bin/sub
install -m 0755 "$src/scripts/geo-update.py" /usr/local/bin/geo-update
install -m 0644 "$src/systemd/"* /etc/systemd/system/
systemctl daemon-reload
echo '4/6 初始化配置；生成独立密码（失败后用同一命令重跑）'
sub init --ip "$ip" --node "$node" --name "$name" --email "$email"
echo '5/6 安装 Certbot，申请 IP HTTPS 证书并启用续期'
if ! snap list certbot >/dev/null 2>&1; then snap install --classic certbot; fi
# Certbot webroot IP 证书要求 >=5.4；版本由 snap 维护。
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
install -m 0755 "$src/scripts/reload-nginx.sh" /etc/letsencrypt/renewal-hooks/deploy/lightsail-vpn-nginx
sub tls
systemctl enable --now snap.certbot.renew.timer
echo '6/6 首次下载 CN MRS，启用服务与每日更新'
geo-update update
sub update
systemctl enable --now nginx hysteria-server.service geo-update.timer
sub status
echo '部署完成。订阅 URL 含私密令牌，仅在你主动执行 sudo sub url 时显示。'
