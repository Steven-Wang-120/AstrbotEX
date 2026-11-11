# 独立加密邮箱部署

这是小规模预览服务，不需要启动机器人或 EX 运动 runtime。使用 Python 3.10+；建议绑定 loopback，由 HTTPS 反向代理公开。保存独立数据目录，不混用原 EX 聊天数据库。

## 安装

```sh
mkdir -p ~/astrc-mailbox/data
python3 -m venv ~/astrc-mailbox/venv
~/astrc-mailbox/venv/bin/pip install -r docs/chat/requirements-peer.lock
cp astrbot_ex/core/chat/peer_relay.py ~/astrc-mailbox/peer_relay.py
~/astrc-mailbox/venv/bin/python ~/astrc-mailbox/peer_relay.py \
  --data-root ~/astrc-mailbox/data --public-url https://mail.example.org
```

默认只监听 127.0.0.1:8781。也可安装 EX `.[chat]` 后用 `python -m astrbot_ex.core.chat.peer_relay`。生产不传 `--allow-loopback`，不使用 `--demo`。运行账号应只能访问其邮箱数据；数据目录权限 0700、文件 0600，systemd 模板 UMask=0077。

将 [deploy/astrc-mailbox.service](deploy/astrc-mailbox.service) 复制至 `~/.config/systemd/user/`，将示例域名改为实际 HTTPS origin，再执行：

```sh
systemctl --user daemon-reload
systemctl --user enable --now astrc-mailbox.service
sudo loginctl enable-linger "$USER"
```

不要覆盖已有 EX/Dashboard/其他 Tailscale 服务。模板路径是独立 `~/astrc-mailbox/`；不含口令、令牌或身份私钥。

## HTTPS 与公网

可用现有域名及 Caddy/Nginx 转发到 loopback，或在节点管理员已允许 Funnel 后：

```sh
sudo tailscale funnel --bg --https=443 http://127.0.0.1:8781
sudo tailscale funnel status
```

使用输出的 HTTPS 根地址作为 public-url 和客户端 `ASTRC_RELAY`。Funnel 转发设在节点上；正常公众客户端无需 Tailscale。该服务没有关闭 TLS 验证。是否在特定地区/运营商可达必须另测，不能保证所有网络均不受限制。

健康检查：`GET /api/v1/peer/health`。在安装了 Tailscale 的电脑上，MagicDNS 可能返回私网 IP；公网验收需使用公共解析器和强制 public-IP 路由并保留域名、SNI、证书校验。astrC 的 `tools/run_peer_roundtrip.py --relay-a https://mail.example.org --connect-ip <public-ingress-ip>` 使用真实 Dart 收发和测试专用 TLS socket；不改系统 DNS/hosts。

## 容量和维护

单进程、独立 SQLite；不支持多个 worker 同时承担挑战/令牌。节点持久数据不含正文解密密钥，但包含联系人许可和元数据，仍应保护。客户端无需管理口令，任何设备可证明新身份；本版仅有有界容量和简单限流，无邀请码、完整防滥用、审计或高可用。`proxy_headers=False`；反代后的所有访客可能共用 loopback 来源限流，适合当前少量用户预览，不能据此称公开规模化部署。

备份时先停止此用户单元，复制整个 data 目录到受保护位置，再启动；不要在 SQLite 写入时仅复制单一文件。节点重启使会话失效但保存密文与联系人许可，设备会重新认证。更新前保留旧源码、虚拟环境版本与数据备份；scp 新文件后重启单元，核对 SHA-256 和健康响应，失败则恢复旧源码。勿重置数据库冒充修复。

保留与限制以 [协议](PEER_PROTOCOL.md) 为准：7 天惰性过期、已入邮箱不等于已读。节点迁移须分享新名片、更新好友地址并自行处理旧邮箱未取信件；不自动转寄。
