# 基础旧安装补齐订阅和统计

只有基础 Hysteria/早期 Geo、缺少 `.sub-state/.sub-node/.sub-token` 和本项目订阅站点时，使用 **migrate.sh**。它有明确的配置变更计划，不是 update.sh 的纯代码替换。安排维护时段，代理会短暂重连。

## 适用范围

- Hysteria password 认证、Salamander、UDP 443。
- 自签证书/私钥路径为 `/etc/hysteria/certs/server.crt`、`server.key`，公网 IP 与证书 SAN 一致。
- Nginx 启用站点只有 default；额外网站、半迁移状态、其他运行用户/证书布局会停止，不猜测覆盖。
- AWS 已绑定静态 IP，开放 TCP 80/443、UDP 443；TCP 80 允许 CA 验证。

迁移保留认证密码、混淆密码、TLS 证书/私钥及附加 Hysteria 设置，不轮换代理秘密。只规范 YAML 引号/顺序并增加本地 trafficStats，不保留注释格式。新统计 secret 和订阅 token 在目标机生成；新订阅 URL 需重新导入。原历史数据库不删除、不重置，AWS 身份不读取/复制。

## 先检查

```bash
git pull --ff-only
sudo apt-get update
sudo apt-get install -y python3-yaml
sudo bash migrate.sh --check \
  --ip <当前公网静态IPv4> \
  --region <当前AWS地区代码> \
  --instance-name <当前Lightsail实例名> \
  --node '新地区节点名' \
  --name migrated-vpn \
  --accept-tos \
  --upgrade-hysteria
```

替换尖括号字段，填写当前实例的信息，不是原韩国实例。migrated-vpn 必须是未占用的新订阅文件名。没有密码或 token 参数，不需要发给助手任何秘密。

邮箱不是必填项。首次申请不填邮箱时，以无邮箱方式登记；如需提供联系邮箱，额外加 `--email 你的邮箱`。已有可续期、匹配 IP 且剩余有效期超过一天的证书直接复用，不改账户邮箱；过期/即将过期或缺少续期配置时再走申请流程。自动续期不依赖提供邮箱。

`--check` 只显示计划，不写备份、不安装软件、不改配置或重启；解析依赖先安装。配置只在本机解析，错误不输出秘密原文。`--accept-tos` 表示已阅读 [Let's Encrypt 条款](https://letsencrypt.org/repository/)，只有 apply 才申请证书。

## 明确执行

确认计划后，把同一命令的 `--check` 换成下面三个选项，其余参数不变：

```text
--apply --replace-legacy-code --archive-old-subscriptions
```

- `--apply`：补齐组件，安装 boto3、必要时安装 snap Certbot、申请 IP HTTPS 证书、下载五份 Geo、重启并验证代理/本地统计 API。
- `--replace-legacy-code`：明确允许先私密备份再替换旧 sub/Geo，不是将未知指纹冒认为可信。
- `--archive-old-subscriptions`：公开 Web 根目录中的旧 `.yaml/.yml` 移入本次 root 私密备份，避免接管默认站点后绕过旧鉴权。旧 URL 需换新 URL，旧文件可恢复；其他目录不动，遇到订阅符号链接停止。

`--upgrade-hysteria` 备份旧二进制并验证官方 SHA256，更新为已核对的 2.12.0，确保统计 API 兼容。确认已经是 2.12.0、不想替换二进制时，可在 check/apply 两步都省略；其他版本要求明确升级。

补齐标准 sub、完整 Geo 缓存、域名采集/TUI/SQLite、AWS 套餐统计及元数据、四次/日 Geo、半小时套餐查询、每日历史维护。保留 Hysteria 自身的 service 单元，原数据库不清空，旧 Geo 单元会替换成仓库设置。新订阅使用仓库的大陆直连/非大陆代理规则。

## 备份和失败恢复

备份在 `/var/backups/lightsail-vpn-migration/<ID>/`，目录 700、文件 600。**包含原配置中的秘密，勿上传 Git 或发到聊天。** 不备份私钥文件本身或 AWS 凭据。

CA 验证、下载或服务启动失败，会尝试恢复程序、配置、旧默认站点、旧公开订阅和服务状态。有效缓存、日志、软件包/账户和新申请的证书可能保留；不是整机事务回滚。成功后备份保留；不能用 update.sh 的代码回退 ID 回退迁移，如需恢复，应在本机根据迁移 manifest 人工恢复。

不要同时运行旧 sub、安装脚本或手动改配置。强杀、断电、磁盘满和恢复重启失败不保证自动恢复成功；保留备份 ID。恢复完成后可修复原因重试；已有订阅状态时拒绝重复接管，不要删除状态绕过保护，也不要运行 deploy.sh 覆盖。

## 迁移后

```bash
sudo sub url
sudo sub status
sudo sub usage status
sudo sub traffic status
sudo bash update.sh --check
```

把新 URL 导入客户端并验证代理、分流、MRS 下载。无 AWS 身份时套餐统计会提示未就绪，按 [README 第 3 节](../README.md#3-手动配置-aws-只读身份) 手动配置。以后日常更新用 update.sh，不再用 migrate.sh。

此入口已做隔离测试，未在你的生产实例执行 apply，仍需实际 ACME、公网 UDP、下载和手机连接验收。
