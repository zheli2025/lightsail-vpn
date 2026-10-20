# Lightsail VPN：原服务复现部署仓库

在另一个地区的全新 **Ubuntu 24.04 LTS Lightsail** 实例上复现当前项目的功能和已核对设置。支持 amd64 / arm64；不修改旧实例、不迁移旧秘密。与第一版精简仓库不同，这个版本恢复原版 `sub`、全量 Geo 缓存、AWS 套餐流量、域名用量采集及 TUI。

完整对照、原版哈希和两处兼容修复见 [docs/PARITY.md](docs/PARITY.md)。实际服务及订阅的非敏感配置已经现场核对，结果见 [config/software-baseline.json](config/software-baseline.json)。复现服务设置、分流规则和流量记录功能；原机密钥、IP、订阅地址不复制，新实例独立生成/填写。流量历史不迁移，这也不是整机磁盘镜像。

## 1. AWS 控制台手动操作

1. 在所需地区新建 Linux/Unix → OS only → Ubuntu 24.04 LTS 实例。记录**地区代码**和**准确实例名**。
2. 在同地区创建并绑定静态公网 IPv4。记录公网 IP，不用 `172.*` 等私有 IP。
3. Networking → IPv4 firewall 开放下列端口。若自行启用了 UFW，也在操作系统中手动放行。脚本不改变 AWS/UFW 防火墙。

| 协议 | 端口 | 用途 |
|---|---:|---|
| TCP | 22 | SSH；按需限制自己的来源 IP，保留控制台 SSH 访问 |
| TCP | 80 | 原版 HTTP 订阅和 IP 证书验证/续期；需保持 CA 可访问 |
| TCP | 443 | HTTPS 订阅和规则文件 |
| UDP | 443 | Hysteria2；与 TCP 443 分别添加 |

不要开放 25413，它只在 `127.0.0.1` 提供域名统计 API。代理/订阅端口的来源范围需覆盖实际客户端；限制 IP 时不要遗漏手机移动网络。[AWS 防火墙文档](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-firewall-and-port-mappings-in-amazon-lightsail.html)。

4. 登录新实例，确认可访问 Ubuntu 软件源、GitHub、snap 商店。准备好下面部署所需的参数。AWS API 的只读身份在第 3 节单独设置，不是创建 VPN 的先决条件。

## 2. clone 后部署

**已经安装旧版服务的实例，请跳到下面“旧实例增量更新”，不要运行 deploy.sh。** `deploy.sh` 用于新实例；`update.sh` 用于保留原设置和数据的代码更新。

```bash
sudo apt-get update
sudo apt-get install -y git
git clone <你的仓库URL> lightsail-vpn
cd lightsail-vpn
bash deploy.sh --help
```

阅读 [Let's Encrypt 条款](https://letsencrypt.org/repository/) 后执行；替换所有尖括号字段，node/name 也可自定义：

```bash
sudo bash deploy.sh \
  --ip <公网静态IPv4> \
  --region ap-northeast-1 \
  --instance-name <AWS控制台的准确实例名> \
  --node '日本-Tokyo-01' \
  --name tokyo-vpn \
  --email <你的联系邮箱> \
  --accept-tos
```

地区示例 `ap-northeast-1` 是东京，请填写你实际选择的地区。实例名必须准确，否则会取不到或取错统计目标。节点名最长 32 字符，不含双引号、反斜杠、控制字符，不用 PROXY/DIRECT 等策略名；订阅名限 1–32 位小写字母、数字、连字符。

部署执行七步：

1. 安装依赖，设定与原机一致的 `Etc/UTC` 时区。
2. 下载与原机一致的 Hysteria **2.12.0**，验证官方 `hashes.txt`。
3. 创建 hysteria、usage-tracker 用户及原项目目录、日志权限。
4. 生成目标机独立凭据、Hysteria 基础配置和本地统计 API；安装原版脚本、systemd 单元及已核对的内核参数。
5. 原版 sub 先生成 HTTP 站点；安装 snap Certbot、申请 IP HTTPS 证书、启用续期后再次生成双协议站点。
6. 首次下载全部五份规则文件，启动 Hysteria、域名采集和定时器。
7. 尝试 AWS 套餐流量更新。未配置 AWS 身份时会明确警告；按第 3 节完成后补齐。

仅用于全新专用实例：检测到已有 VPN 配置或非本仓库的运行服务会拒绝接管；第一版精简仓库已经部署的实例也不直接接管。网络失败后可用**同一组参数**重跑，保留已有凭据和统计数据。参数变更使用 `sub` 管理命令，不通过改部署命令迁移。

初次 Geo 下载失败会让部署退出；修复后重跑，不能把失败当作成功。脚本只固定 Hysteria 版本，不固定所有 Ubuntu、Nginx、Python、snap 包版本；不会从旧服务器拷贝二进制或账户资料。

## 3. 手动配置 AWS 只读身份

VPN 本身不用 AWS 凭据；**套餐配额/用量显示**需要目标机上的 boto3 能访问对应 Lightsail 实例。域名用量使用本地 API，不需要 AWS 权限。

在 IAM 创建/使用独立只读身份，附加 [docs/aws-readonly-policy.json](docs/aws-readonly-policy.json)，只允许 `GetInstance`、`GetInstanceMetricData`。可进一步按 AWS 支持情况限制地区等条件。没有创建实例、改防火墙或更换 IP 的权限。[AWS Lightsail 权限参考](https://docs.aws.amazon.com/service-authorization/latest/reference/list_lightsail.html)。

脚本使用标准 AWS SDK 凭据链。若使用访问密钥，由你在**新实例**手动配置 root 的默认 profile（服务和 sudo sub 以 root 运行）：

```bash
sudo install -d -m 0700 /root/.aws
sudoedit /root/.aws/credentials
```

文件内容由你填写，勿发到聊天或写进仓库：

```ini
[default]
aws_access_key_id = <新只读身份的AccessKeyID>
aws_secret_access_key = <新只读身份的SecretAccessKey>
```

临时凭据还需 `aws_session_token`，并由你安排续期。保存后：

```bash
sudo chmod 0600 /root/.aws/credentials
sudo systemctl start traffic-update.service
sudo sub traffic status
```

不要复制当前服务器的密钥。已有安全的临时身份提供方式时可继续用标准 boto3 配置；这里只给出可手动完成的默认 profile 方式。原版 status 的提示可能提到 EC2 风格实例角色，但不要把 Lightsail 服务关联角色当作应用可自动获得的身份；本部署不假定已附加此类角色。[AWS 身份说明](https://docs.aws.amazon.com/lightsail/latest/userguide/security_iam.html)。

运行参数在 `/etc/systemd/system/traffic-update.service.d/migrate.conf`。它只包含地区/实例名/IP，不放密钥。薄启动器使 timer 和手动 `sub traffic` 都读取它；`sub ip` 更新后两者保持一致。

## 4. 导入客户端和验收

```bash
sudo sub status
sudo sub url
sudo sub geo status
sudo sub traffic status
sudo sub usage status
sudo nginx -t
systemctl list-timers geo-update.timer traffic-update.timer domain-usage.timer snap.certbot.renew.timer
```

URL 含私密令牌；原版 `sub update/name/node/token/rotate/ip` 也可能输出该 URL，不要分享终端日志。导入 Clash Mi、Clash Verge Rev 或 Clash Meta for Android 的近期 Mihomo 内核版本，更新订阅、选中节点，使用 rule 模式。PROXY 组保留 DIRECT 选项，日常需选代理节点。

从手机移动网络或另一台机器验证：HTTP/HTTPS 的订阅无 token 返回 403、带正确 token 可下载；`/geo/geosite-cn.mrs`、`/geo/geoip-cn.mrs` 能下载；代理出口是新服务器、大陆访问按规则直连。不能只靠 VPS 自测判断 UDP 公网可达。

验证 `domain-usage.service`、Hysteria、Nginx、各 timer 为 active。产生一些代理 TCP 流量后看 `sudo sub usage` 和 `sudo sub usage tui` 是否有数据。没有流量时空表正常；采集只看每 5 秒的活跃 TCP 流快照，短连接、UDP 流量可能漏记，不等于完整账单计量。AWS 用量按原脚本统计月初 UTC 起的 NetworkIn+NetworkOut，不宣称等于 AWS 最终计费账单。

## 5. 原版管理命令

```bash
sudo sub help
sudo sub update
sudo sub name new-name
sudo sub node '新节点名'
sudo sub token
sudo sub rotate
sudo sub geo update
sudo sub traffic update
sudo sub usage today
sudo sub usage month --json
sudo sub usage tui
```

`token` 会轮换订阅令牌，需要在客户端换 URL；`rotate` 会轮换代理及混淆密码并重启，客户端需刷新订阅；`name` 会改变订阅路径；`update` 不下载 Geo、不自动轮换密码。

规则与原机一致：大陆域名直连，`cn-ip` 带 `no-resolve`，其他走 PROXY。VPS 北京时间 00/06/12/18 点更新全部五份 Geo；客户端 MRS 每 86400 秒刷新。该版本按原机实际设置每天四次，并非上一版仓库的每天一次。完整 Geo 保留在服务器，但客户端依然只引用两个 MRS，不因此加载全量 DAT/MMDB。

## 6. 更换静态 IP

先在 AWS 绑定新静态 IPv4，检查 TCP 80/443、UDP 443。通过新 IP SSH 登录同一实例：

```bash
sudo sub ip <新公网IPv4>
sudo sub status
sudo sub url
sudo sub traffic update
```

原版命令依次申请新 IP HTTPS 证书、备份并更换自签证书、重启 Hysteria、更新订阅/Nginx/流量 IP、清理旧 IP 证书。认证密码、混淆密码和订阅 token 保留，所有客户端需要改 URL 主机并刷新。

保留原版的分阶段回滚行为，不等于整个迁移事务性：自签证书更新后若后续步骤失败，可能处于部分完成状态。必要时修复错误后 `sudo sub update`；若需回退外部 IP，必须在 AWS 手动重新绑定。不要在新证书/订阅未就绪时反复运行同 IP 的 `sub ip`，原版会拒绝“与当前 IP 相同”。

首次部署参数文件仅用于重入保护，不随日常改名/IP 更新。迁移后不要用旧参数重跑 deploy 来恢复服务；用 `sub`。灾难恢复应在新实例重新部署。

## 7. HTTPS 和安全行为

无域名 IP HTTPS 使用约 6 天有效的 Let's Encrypt 短期证书，snap Certbot 自动续期并运行 Nginx reload 钩子；TCP 80 要长期允许 CA 验证。webroot IP 证书需要 Certbot 5.4 或更新版本。[Let's Encrypt 官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

```bash
sudo /snap/bin/certbot renew --dry-run
sudo journalctl -u snap.certbot.renew.service --since '24 hours ago'
sudo journalctl -u hysteria-server.service -n 50 --no-pager
sudo journalctl -u geo-update.service -n 50 --no-pager
sudo journalctl -u traffic-update.service -n 50 --no-pager
sudo journalctl -u domain-usage.service -n 50 --no-pager
```

证书失败检查 IP 绑定、TCP 80、ACME 路径和 `/var/log/letsencrypt/letsencrypt.log`。不要高频重复申请以免触发 CA 限额。日志分享前脱敏；查看日志不需要把密钥文件发给任何人。

为复现原服务，保留 HTTP 和公开 webroot 结构，**不会自动升级为 HTTPS-only 或“公开目录之外的订阅”**。日常使用 HTTPS URL；HTTP 会明文传输 token 和订阅。原版精确路径有 token 校验，但公开 webroot 的其他副本/备份不自动保护，勿把任何订阅备份、临时秘密文件放进该目录。Hysteria UDP 仍为独立自签证书、`skip-cert-verify: true`，不等同于订阅 HTTPS 的受信任身份验证。

## 8. 文件、测试和 Git

运行路径与原项目一致：`/etc/hysteria/config.yaml`、`/etc/hysteria/.sub-*`、`/etc/hysteria/.traffic-stats-secret`、`/var/www/html/<name>.yaml`、`/var/www/html/geo/`、`/var/lib/traffic/`、`/var/lib/domain-usage/`、`/etc/nginx/sites-available/vpn-subscription`。这些运行文件均不存放在 clone 目录。配置/权限详情见 [复现记录](docs/PARITY.md)。

部署参数文件 `/etc/lightsail-vpn/install.json` 不含密码。它是防止误接管/误改参数的安装标记，不是用来手改服务配置的源文件。日常使用原版 `sub`。

本地验证只使用临时目录和模拟服务调用，不读取生产密钥或调用 AWS：

```bash
bash -n deploy.sh update.sh migrate.sh scripts/sub.sh scripts/geo-update.sh scripts/reload-nginx.sh
python3 -m unittest discover -s tests -v
```

测试还检查原版哈希和两处已记录的报表/清理修复；不代替新实例的 ACME、公网 UDP、手机实际连接验收。固定旧版本用于复现，不能据此认定它永远是最新或最安全版本；升级需单独检查和验证。

你可以在此独立仓库手动设置远程和 push：

```bash
git remote add origin <你的仓库URL>
git push -u origin main
```

不要从父目录提交原服务器运行数据。`.gitignore` 排除常见私钥、订阅 YAML、状态、数据库、AWS 凭据和日志，但不代替提交前检查。

## 9. 旧实例增量更新

按组件识别本项目的旧安装，不要求订阅、Geo、AWS 套餐统计、域名采集全部安装，也不要求本仓库的安装标记。先确认存在 `/etc/hysteria/config.yaml` 和 Hysteria 服务单元，再独立判断每个组件：能够安全识别的组件可增量更新，缺失或布局不同的组件提示后跳过，保留原服务。第一版精简 Python sub 或第三方脚本也不会被强行转换；请勿运行 deploy.sh 覆盖旧实例。

在已有实例上 clone 到独立目录，或者已有 clone 执行 `git pull --ff-only`，然后：

```bash
cd lightsail-vpn
sudo bash update.sh --check
sudo bash update.sh --apply
```

省略参数也只做检查。检查不更改目标文件、不备份、不重启，不读取密码/订阅/数据库/AWS 凭据，只检查运行文件存在性、程序指纹和地区/实例元数据。较早安装没有 `.sub-state/.sub-node/.sub-token` 等文件时，会保留它的旧 sub；没有域名或套餐统计时，会提示这些组件尚未安装，而不是报整个安装不兼容。

缺失组件不会在增量更新时自动创建/启用。尤其不能随意新建 `.sub-token` 或调用初始化来“修复检查”，否则可能破坏已有订阅和密码。要把基础旧服务补齐为完整服务，使用 [基础旧安装迁移指南](docs/MIGRATE.md) 中的 `migrate.sh`，不能只靠放宽检查就宣称迁移完成。

`待确认` 表示某个已安装程序是未收录旧版或有本地改动：`--check` 会列出，`--apply` 仍会停止，不盲目覆盖。如果当前没有可自动更新的代码，检查会明确说明，不表示缺失功能已经安装。

默认更新已识别组件对应的 sub、geo-update、traffic-update、domain-usage 程序文件；流量统计组件完整时新增薄启动器，以便手动查询和 timer 使用同一地区/实例参数。**保留原服务 YAML、密钥、订阅路径/token、Nginx 站点、systemd 单元及 drop-in、Geo 缓存、流量历史、SQLite、AWS 身份和证书。** 不运行初始化、sub update、证书申请或 Geo 下载，也不重装/升级 Hysteria/Nginx、不改防火墙、时区和 sysctl。因此现有分流与订阅地址不会被重建，更新后可继续用原链接。

程序文件与仓库相同则跳过。仅在域名采集代码改变且采集器原本运行时重启它，默认不会重启 Hysteria；可能产生短暂采集间隙。若 Geo/AWS/清理一次性任务正在运行，先等它结束再重试。更新期间避免并发运行 sub 或安装脚本；更新器锁住部署入口，但不能阻止旧脚本或 timer 在检查后启动任务。

若明确需要把**服务单元/定时器**同步成当前仓库版本，先检查，再显式添加参数：

```bash
sudo bash update.sh --check --update-units
sudo bash update.sh --apply --update-units
```

此模式可能改变原版默认定时设置，必要时会重启受影响且原本 active 的服务/timer；更新 Hysteria 单元可能短暂断线。仍不覆盖 drop-in。自定义单元或程序若无法根据原版哈希/上次更新记录识别，更新器停止，不自动覆盖；应先人工审查，默认更新仍保留自定义单元。

每次实际更新都会把涉及的**程序/单元**备份到 root 私有目录 `/var/backups/lightsail-vpn-update/<ID>/`；不复制密钥或数据库。发生安装/重启错误会尝试恢复这些文件。成功时终端打印备份 ID，需要时回退：

```bash
sudo bash update.sh --rollback <备份ID>
```

回退只恢复当次替换的代码/单元，不重置配置或流量历史。若之后又改了程序或又更新过，应按时间逆序回退；检测到文件变化会停止，避免覆盖新改动。强杀、断电、磁盘损坏或回退重启失败不保证自动恢复成功，保留备份用于人工恢复。

先阅读检查输出，只更新已核对的这套安装。此更新器已用临时目录测试，但没有在你的生产服务上执行 `--apply`。
