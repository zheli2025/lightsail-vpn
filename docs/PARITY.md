# 原服务复现范围与核对依据

核对日期：2026-10-20。复现范围是软件服务配置、分流规则、流量记录及管理行为；不包括原实例密钥、IP、订阅地址或历史数据。

已核对项目源码、已安装可执行程序的哈希、非敏感 systemd 单元、时区及五项内核参数，并现场提取了 Hysteria 配置及当前订阅中的非敏感设置。过滤在本机完成：不输出或复制密码、token、统计 API secret、节点/订阅地址；不读取私钥文件、AWS 凭据文件或 SQLite 历史数据。核对结果保存于 `config/software-baseline.json`。

`baseline.sha256` 保存原机版本的哈希。原机 `/usr/local/bin/sub`、`geo-update`、`traffic-update`、`domain-usage` 与父项目对应源文件哈希一致。7 个 Geo/统计 systemd 文件也与原机安装文件一致。仓库使用这些源码，不再使用第一版独立 Python 订阅管理器。

| 设置 | 本仓库 |
|---|---|
| Hysteria | 原机版本 2.12.0；UDP 443；Salamander；独立自签证书 |
| Hysteria service | hysteria 用户；RestartSec=3；LimitNOFILE=1048576；NET_BIND_SERVICE |
| 订阅与规则 | 原版 sub 生成；rule 模式；客户端 IPv6=false；私有地址 DIRECT；CN 域名 DIRECT；CN IP DIRECT,no-resolve；MATCH PROXY |
| PROXY 组 | 单节点 + DIRECT，保留人工切换 |
| 订阅 HTTP/HTTPS | 两者保留；token 鉴权、请求限速；HTTPS IP 证书、snap 自动续期、Nginx reload 钩子 |
| 规则服务器缓存 | geoip.dat、geosite.dat、country.mmdb、geosite-cn.mrs、geoip-cn.mrs；原下载源和校验逻辑 |
| VPS 规则更新时间 | 北京时间 00/06/12/18 点；随机 0–120 秒；启动后 10 分钟；Persistent |
| 客户端 MRS 刷新 | interval=86400 |
| AWS 流量统计 | 每 30 分钟；月初 UTC 起 NetworkIn+NetworkOut；套餐配额及订阅响应头；Profile-Update-Interval=12 小时 |
| 域名用量 | 本地 127.0.0.1:25413 /dump/streams；5 秒轮询；SQLite；分类、报表、TUI |
| 数据维护 | 每天 UTC 03:15 加最多 30 分钟；明细保留 180 天；启动后 5 分钟 |
| IP 更换 | 原版 sub ip，含流量统计 IP 同步和旧证书自动清理 |
| 时区 | Etc/UTC |
| UDP 缓冲区上限 | rmem_max / wmem_max 均为 4194304 |
| OS 网络 | ip_forward=0；OS IPv6 disable_ipv6=0，客户端 IPv6 仍关闭 |

需要随服务器改变：公网 IP、AWS region、实例名、节点显示名称、订阅路径名、ACME 联系邮箱。原脚本里的旧服务器回退地址保留以保持源码一致；正常部署由自签证书 SAN 和服务 Environment 提供新实例信息，不使用回退地址。不要单独拷贝运行脚本到未初始化实例上执行。

必须重新生成/手动配置：认证密码、混淆密码、订阅 token、统计 API secret、自签证书、IP HTTPS 证书、AWS 身份。历史流量/数据库不迁移。显式 LS_INSTANCE_NAME 优先定位实例，因此不依赖原服务器的 PRIVATE_IP。

为支持不同地区，`/usr/local/bin/traffic-update` 是一个读取同一份 `migrate.conf` 的薄启动器，原版核心脚本安装在 `/usr/local/lib/lightsail-vpn/traffic-update.py`。这样 `sub traffic update` 手动调用也使用正确实例，而不只有 systemd timer 获得 Environment 参数；`sub ip` 更新该文件后立即对两者生效。这是部署参数适配，不改变统计算法。

## 实际配置核对结论

当前 Hysteria 的顶层配置只有 listen、tls、auth、obfs、trafficStats；没有额外 resolver、sniff、ACL、带宽或 QUIC 参数。仓库初始化配置的结构和所有非敏感值与实际配置一致。当前生效的 Clash 订阅也已逐项核对：基础选项、协议/端口、PROXY 组、两个 MRS provider 的格式/路径/间隔及全部规则均一致，仅替换新实例的名称、地址和秘密。

域名采集、Geo 更新和 Hysteria 服务没有额外 systemd drop-in；套餐流量服务只有用于地区/实例参数的 migrate.conf。三个 timer 的现场定义与仓库文件一致。流量记录的算法、分类、5 秒采集、SQLite 结构、保留期、TUI 和订阅头仍使用同源代码；只保留下面记录的两处报错修复。

因此无需另行提供脱敏配置。软件配置与功能行为的复现核对已经完成；新实例仍需实际部署及联网验收。不复制整机磁盘，也不锁定所有 Ubuntu/apt/snap 包版本。

本次操作仅修改这个 Git 仓库，未把任何改动安装到当前生产服务。

## 两处针对原代码错误的兼容修复

`scripts/domain-usage.py` 在原版基础上仅修复以下执行错误，不改变设置、分类规则、数据库结构或报表口径：

1. `report ... --json` 原版引用未定义的 `conn`，改为单独读取 meta 后关闭连接。
2. `prune` 原版在 DELETE 尚未提交的事务中执行 WAL checkpoint，可能报 database table is locked，改为提交后 checkpoint。

因此 domain-usage 的当前哈希与 `baseline.sha256` 中的原版不同。测试会反向应用这两处修复验证仍与原版一致；其他三个核心脚本及七个单元保持原版哈希。原服务器源码与安装文件未修改。
