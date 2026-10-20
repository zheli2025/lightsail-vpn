#!/usr/bin/env bash
# 全新 Lightsail Ubuntu 24.04：复现原项目运行设置，不迁移旧实例的凭据或数据库。
set -euo pipefail
umask 077
src="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
die() { echo "error: $*" >&2; exit 1; }
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  python3 "$src/scripts/bootstrap.py" --help
  exit 0
fi
[[ $EUID == 0 ]] || die '请用 sudo bash deploy.sh 运行'
# 校验参数不读取任何配置；在安装软件/写入系统前执行。
python3 "$src/scripts/bootstrap.py" --validate-only "$@"
source /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || die '只支持全新 Ubuntu 24.04 LTS'
exec 9>/run/lightsail-vpn-deploy.lock
flock -n 9 || die '部署已经在运行'
if [[ ! -f /etc/lightsail-vpn/install.json ]]; then
  for path in /etc/hysteria/config.yaml /usr/local/bin/sub /etc/nginx/sites-enabled/vpn-subscription; do
    [[ ! -e "$path" ]] || die "已有安装 $path；拒绝接管，请使用全新实例"
  done
  if systemctl is-active --quiet nginx || systemctl is-active --quiet hysteria-server; then
    die '检测到已有服务，请使用全新专用实例'
  fi
fi
echo '1/7 安装依赖并统一为原机 UTC 时区'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nginx openssl curl ca-certificates python3 python3-boto3 snapd
timedatectl set-timezone Etc/UTC
echo '2/7 安装原机版本 Hysteria 2.12.0，核对官方 SHA256'
case "$(uname -m)" in x86_64) arch=amd64;; aarch64) arch=arm64;; *) die '仅支持 amd64/arm64';; esac
tmp="$(mktemp -d /tmp/lightsail-vpn-release.XXXXXX)"
trap 'rm -f "$tmp/hysteria-linux-$arch" "$tmp/hashes.txt"; rmdir "$tmp"' EXIT
base='https://github.com/apernet/hysteria/releases/download/app/v2.12.0'
curl -fSL --retry 3 --connect-timeout 15 --max-time 600 "$base/hysteria-linux-$arch" -o "$tmp/hysteria-linux-$arch"
curl -fSL --retry 3 --max-time 120 "$base/hashes.txt" -o "$tmp/hashes.txt"
python3 "$src/scripts/verify-release.py" "$tmp" "hysteria-linux-$arch"
install -m 0755 "$tmp/hysteria-linux-$arch" /usr/local/bin/hysteria
echo '3/7 创建服务用户、原项目目录及统计所需权限'
id hysteria >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin hysteria
id usage-tracker >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin usage-tracker
install -d -m 0700 /etc/lightsail-vpn
install -d -m 0755 /usr/local/lib/lightsail-vpn
install -d -m 0755 /etc/hysteria /etc/hysteria/certs /var/www/html /var/www/html/geo
install -d -m 0700 /var/lib/geo-update /var/lib/traffic
install -d -m 0750 -o usage-tracker -g usage-tracker /var/lib/domain-usage
touch /var/log/geo-update.log /var/log/traffic-update.log /var/log/domain-usage.log
chmod 0600 /var/log/geo-update.log /var/log/traffic-update.log
chown usage-tracker:usage-tracker /var/log/domain-usage.log
chmod 0640 /var/log/domain-usage.log
touch /etc/nginx/subscription-userinfo.conf
chmod 0644 /etc/nginx/subscription-userinfo.conf
echo '4/7 生成新实例独立凭据及地区/实例参数；不覆盖已有配置'
python3 "$src/scripts/bootstrap.py" "$@"
install -m 0755 "$src/scripts/sub.sh" /usr/local/bin/sub
install -m 0755 "$src/scripts/geo-update.sh" /usr/local/bin/geo-update
install -m 0755 "$src/scripts/traffic-update.py" /usr/local/lib/lightsail-vpn/traffic-update.py
install -m 0755 "$src/scripts/traffic-launcher.py" /usr/local/bin/traffic-update
install -m 0755 "$src/scripts/domain-usage.py" /usr/local/bin/domain-usage
install -m 0644 "$src/systemd/"* /etc/systemd/system/
install -m 0644 "$src/config/99-lightsail-vpn.conf" /etc/sysctl.d/99-lightsail-vpn.conf
sysctl -p /etc/sysctl.d/99-lightsail-vpn.conf
systemctl daemon-reload
systemctl enable --now nginx
# 原版 sub update 生成 HTTP 站点，提供 Certbot webroot 验证路径。
/usr/local/bin/sub update
echo '5/7 IP HTTPS：安装 Certbot、申请短期证书、启用自动续期'
if ! snap list certbot >/dev/null 2>&1; then snap install --classic certbot; fi
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
install -m 0755 "$src/scripts/reload-nginx.sh" /etc/letsencrypt/renewal-hooks/deploy/lightsail-vpn-nginx
python3 "$src/scripts/bootstrap.py" --tls "$@"
systemctl enable --now snap.certbot.renew.timer
/usr/local/bin/sub update
echo '6/7 首次下载全部五份 Geo 数据，启动 Hysteria 和域名采集器'
/usr/local/bin/geo-update update
systemctl enable --now hysteria-server.service
systemctl restart hysteria-server.service
systemctl is-active --quiet hysteria-server.service
systemctl enable --now domain-usage.service domain-usage.timer geo-update.timer traffic-update.timer
systemctl restart domain-usage.service
echo '7/7 首次获取 AWS 流量（需要你在新实例手动配置只读 AWS 身份）'
if ! systemctl start --wait traffic-update.service; then
  echo 'warning: AWS 流量获取失败；VPN 已安装，但订阅套餐流量尚未就绪。按 README 配置身份后运行 sudo systemctl start traffic-update.service。'
fi
/usr/local/bin/sub status
echo '部署步骤完成。仍需按 README 验证公网 TCP/UDP、订阅及域名采集。'
echo '查看含令牌的订阅链接：sudo sub url。部分原版 sub 操作也会输出该链接，请勿分享终端日志。'
