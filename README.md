# Lightsail Hysteria2 单节点 VPN

在任意 AWS Lightsail 地区的**全新 Ubuntu 24.04 LTS** 实例上部署一个 Hysteria2 节点，支持 x86_64 / ARM64。clone 后运行部署脚本即可。无需域名，也无需把 AWS 密钥交给脚本。

默认配置：IPv6 关闭；私有地址直连；大陆域名和大陆 IP 直连；其余走本机 Hysteria2。CN-only MRS 在 VPS 缓存，服务端每天更新，客户端每 86400 秒更新。不下载全量 GeoSite/GeoIP 数据。兼容支持 MRS 的 Mihomo 内核客户端，例如近期版本的 Clash Mi、Clash Verge Rev、Clash Meta for Android；旧 Clash 内核需要先升级。

这是当前服务逻辑的独立部署版本，**不支持直接接管旧安装**。每台服务器独立生成密码、混淆密码、订阅令牌，不复制原服务器凭据。默认不包含需要 AWS API 凭据的套餐流量统计，也不包含域名使用量采集。多地区部署会得到多个独立订阅，本仓库不自动聚合它们。

## 1. 在 AWS 控制台手动完成

1. 在所需地区创建 Lightsail 实例：Linux/Unix → OS only → Ubuntu 24.04 LTS。按流量、价格和性能选择套餐。
2. 在该地区创建静态 IPv4，并绑定到实例。记录绑定后的公网静态 IP。不要填写 `172.*` 等私有地址。
3. 实例 Networking → IPv4 firewall 添加如下规则：

| 协议 | 端口 | 用途 |
|---|---:|---|
| TCP | 22 | SSH；尽量限定自己的来源 IP，或保留 Lightsail 浏览器 SSH |
| TCP | 80 | Let's Encrypt 初次验证及持续续期，必须保持公网可达 |
| TCP | 443 | HTTPS 订阅与规则文件 |
| UDP | 443 | Hysteria2 代理 |

TCP 443 和 UDP 443 可以同时使用。客户端访问位置可能变化时，代理和订阅端口要允许这些来源。TCP 80 需让 CA 验证服务器可达。若操作系统已启用 UFW，也要手动允许上述端口；脚本不会修改任何防火墙规则。

4. 使用控制台 SSH 或自己的 SSH 密钥登录新实例，确认能访问 GitHub、Ubuntu 软件源和 snap 商店。SSH 私钥留在你自己的电脑，不上传仓库。

脚本不创建实例、不绑定 IP、不改 AWS 防火墙、不读 `.aws`，不需要 IAM 用户或 AWS API key。[AWS 防火墙说明](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-firewall-and-port-mappings-in-amazon-lightsail.html)。

## 2. clone 并部署

把本地仓库推送到你自己的 Git 托管服务后，在**新实例**运行：

```bash
sudo apt-get update
sudo apt-get install -y git
git clone <你的仓库URL> lightsail-vpn
cd lightsail-vpn
bash deploy.sh --help
```

先阅读 [Let's Encrypt 服务条款](https://letsencrypt.org/repository/)，然后替换 IP、节点名、邮箱执行：

```bash
sudo bash deploy.sh \
  --ip <绑定后的公网静态IPv4> \
  --node '日本-Tokyo-01' \
  --name tokyo-vpn \
  --email <你的联系邮箱> \
  --accept-tos
```

不要保留尖括号。邮箱用于 ACME 账户联系方式，不能代替服务器上的续期监控。节点名可中文、空格，需加引号；不要使用 `PROXY` / `DIRECT` 等策略名。订阅名限定小写字母、数字、连字符，最长 32 位。

脚本执行六步：安装依赖 → 下载固定版本 Hysteria 2.13.0 并核对官方 `hashes.txt` → 创建低权限服务用户和 systemd 单元 → 生成独立凭据及配置 → 安装 snap Certbot、申请 IP HTTPS 证书、启用续期 → 首次下载 CN MRS、启动服务及每日更新。

脚本检测到旧 Hysteria 配置、旧 `sub` 或正在运行的非本仓库服务，会拒绝部署。它需要专用新实例，不用于共享已有网站的 Nginx。初始化后会移除 Nginx 默认站点的启用链接，配置专用订阅站点。

如中途网络失败，修复后可用**完全相同参数**重跑：已有密码、令牌保持不变。首次规则下载失败会让脚本退出，不能当作部署成功。初始化后不要修改命令中的 IP 或名字来迁移，使用后面的 `sub` 命令。

部署不把密码写入 clone 目录，也不默认在终端输出凭据。完成后取出订阅链接：

```bash
sudo sub url
```

这条命令会显示包含令牌的 URL，勿分享日志、截图或上传到公开仓库。把 URL 添加到 Clash Mi / Clash Verge Rev / Clash Meta for Android，更新订阅并选中节点，使用 rule 模式；不要将 `PROXY` 选择为 `DIRECT`。

## 3. 验收与日常管理

```bash
sudo sub status
sudo geo-update status
sudo nginx -t
systemctl list-timers geo-update.timer snap.certbot.renew.timer
```

安装成功后应看到 Nginx、Hysteria、每日规则 timer 和证书续期 timer 为 active。还需从手机移动网络或另一台服务器验证：无 token 的 `https://<IP>/<订阅名>.yaml` 返回 403，两个 `/geo/*.mrs` 可以下载，带正确 token 的订阅可以更新。然后实际连接代理、检查海外出口 IP 和大陆访问。服务器自测不证明公网 UDP 可达。

| 命令 | 行为 |
|---|---|
| `sudo sub --help` | 查看参数 |
| `sudo sub status` | 查看 IP、名称、服务状态、HTTPS 证书到期时间，不输出密码 |
| `sudo sub url` | 显示含令牌的 HTTPS 订阅 URL |
| `sudo sub update` | 按本机状态重新生成订阅/Nginx，不拉取规则、不轮换密码 |
| `sudo sub name my-vpn` | 修改订阅路径；客户端需替换 URL |
| `sudo sub node '美国-US-01'` | 修改节点名；客户端更新订阅 |
| `sudo sub token` | 生成新订阅令牌；旧 URL 失效，客户端需替换 URL |
| `sudo sub rotate` | 轮换认证和混淆密码、重启 Hysteria；客户端需更新订阅，会短暂断线 |
| `sudo sub geo update` | 手动刷新 VPS 规则缓存 |
| `sudo geo-update status` | 查看文件大小、SHA256、每日 timer |
| `sudo sub tls` | 申请/确认当前 IP 的 HTTPS 证书并重建 Nginx 配置 |
| `sudo sub ip <新公网静态IPv4>` | 完成下面的 IP 迁移 |

订阅刷新和规则刷新是两个独立任务。VPS 规则每天 UTC 03:00 加随机 0–15 分钟更新；客户端另按 86400 秒刷新，因此不保证每次手机更新订阅就立刻拿到当天最新规则。缓存更新使用 HTTPS 来源、Zstd 完整流校验及单文件原子替换；所有来源失败保留旧文件。该校验不是上游规则内容的签名校验，也不保证 CN 两个文件为同一发布快照。

CN 域名先判断，CN IP 兜底允许 DNS 解析；这样对未进入大陆域名集、但解析到大陆 IP 的目标仍有直连机会。效果取决于规则数据及客户端 DNS，无法承诺 100% 地理归属。MRS 降低全量 Geo 数据加载风险，但不是 iOS 内存占用保证；客户端版本、连接数量和其他配置也会影响内存。

## 4. 更换静态 IP

先在 AWS 把**新静态 IPv4 绑定到同一个实例**，确认 TCP 80/443、UDP 443 规则仍在；用新 IP 重新 SSH 登录，再运行：

```bash
sudo sub ip <新公网静态IPv4>
sudo sub status
sudo sub url
```

命令先为新 IP 申请 HTTPS 证书，成功后更新 Hysteria 自签证书、节点 server、规则 URL、Nginx 证书路径及持久状态，重启 Hysteria。订阅密码和令牌不变，但 URL 主机变了，需要在所有客户端替换 URL 并更新订阅。证书申请失败不会修改本地运行配置；服务配置验证/重启失败会尽力恢复旧文件与服务。旧 IP 已解绑时，恢复文件并不能恢复外部访问，需你在 AWS 重新绑定旧 IP，或修复错误后重跑相同 `sub ip` 命令。

旧 HTTPS 证书保留便于回退。新 IP 确认稳定后，可手动删除旧证书，停止它的无效续期任务：

```bash
sudo /snap/bin/certbot delete --cert-name <旧公网IPv4>
```

不要删除当前使用的证书。管理命令带排他锁和失败回滚，但强杀进程、断电或磁盘损坏不保证事务性；必要时运行 `sudo sub update` 修复生成文件。AWS IP 操作不由脚本代办。

## 5. HTTPS 续期及排错

订阅/规则使用 Let's Encrypt IP 证书，无需域名。它是约 6 天有效的短期证书，必须让 snap Certbot 定时续期、TCP 80 保持可达；续期成功会执行本仓库钩子检查并 reload Nginx。Certbot webroot IP 支持要求版本至少 5.4，部署使用 snap 提供的版本，并要求 `shortlived` profile。[Let's Encrypt 官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

Hysteria UDP 使用独立自签证书，客户端沿用 `skip-cert-verify: true`，其身份验证不等同于订阅 HTTPS 的受信任证书验证。勿混淆两者。

可在新实例部署后主动测试 ACME 续期（需要联网验证，可能花几分钟）：

```bash
sudo /snap/bin/certbot renew --dry-run
sudo journalctl -u snap.certbot.renew.service --since '24 hours ago'
sudo journalctl -u hysteria-server.service -n 50 --no-pager
sudo journalctl -u geo-update.service -n 50 --no-pager
```

- 证书申请失败：核对绑定的公网 IP、AWS TCP 80、防火墙、Nginx ACME 路径和 `/var/log/letsencrypt/letsencrypt.log`，然后重跑部署或 `sub tls`。不要高频重复申请，以免触发 CA 限额。
- 订阅可下载但代理不通：核对 AWS **UDP** 443、Hysteria 状态、客户端是否更新密码及选择正确节点。
- 订阅可下载但规则失败：确认两个 MRS 都存在，执行 `sudo geo-update update` 并检查日志。
- HTTPS 过期：检查续期 timer 和 TCP 80；修复后运行 `sudo sub tls`，检查实际 `notAfter`。

日志可能包含访问或账户信息；排错时在你自己的实例查看，分享前脱敏。此仓库不会采集或上传它们。

## 6. 文件位置及仓库维护

| 位置 | 内容 |
|---|---|
| `/etc/lightsail-vpn/state.json` | root 600，持久状态及密码/令牌的来源；不提交 Git |
| `/etc/hysteria/config.yaml` | root:hysteria 640，服务配置 |
| `/etc/hysteria/certs/` | Hysteria 独立自签证书和私钥 |
| `/var/lib/lightsail-vpn/subscription.yaml` | root:www-data 640，公开 webroot 外的订阅 |
| `/var/lib/lightsail-vpn/geo/` | CN MRS 缓存 |
| `/etc/nginx/sites-available/lightsail-vpn` | 包含令牌的运行配置；不提交 Git |
| `/etc/letsencrypt/` | ACME 账户和 HTTPS 私钥；不提交 Git |

日常管理用 `sub`；不要手改生成的 YAML/Nginx 配置。Hysteria 版本固定在 `deploy.sh`；仓库不是自动软件升级器。需要升级时先检查官方发布说明，改版本、验证并在新实例测试。重跑部署会重新安装仓库中固定版本。

本地验证无需部署，也不读取已运行服务的配置：

```bash
bash -n deploy.sh scripts/reload-nginx.sh
python3 -m unittest discover -s tests -v
```

依赖 Python 3 和 PyYAML（Ubuntu 包 `python3-yaml`）。测试用临时目录/模拟服务调用覆盖分流、令牌、IP 更新和失败回滚；不代表已经在新地区完成真实部署、ACME 签发或手机联网测试。

当前仓库已在本地初始化；你可以手动推送：

```bash
git remote add origin <你的仓库URL>
git push -u origin main
```

仅推送这个独立仓库，不要从父目录把原项目及运行数据一起加入。`.gitignore` 排除了常见私钥、YAML、状态和 AWS 凭据文件；它不是秘密扫描器，提交前仍需检查 `git diff --cached`。
