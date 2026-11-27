# 占位符说明（按文件）

> JSON 文件写不了注释，这里按文件列出每个占位符的含义。
> 全仓库占位符统一 `<全大写下划线>` 格式，总表见 README 第 1 节。

## templates/server_config.json

| 占位符 | 含义 |
|---|---|
| `<APP_NAME>` | 应用名 / 部署代号 |
| `<TELEMETRY_DOMAIN>` | 推流域名 |

## templates/server_config.funnel-example.json（Funnel 路线）

| 占位符 | 含义 |
|---|---|
| `<APP_NAME>` | 应用名 / 部署代号 |
| `<TELEMETRY_DOMAIN>` | 推流域名，这里是 `<MACHINE_NAME>.<TAILNET_NAME>.ts.net` |

## templates/tailscale-cert-renew.sh（Funnel 路线）

| 占位符 | 含义 |
|---|---|
| `<TELEMETRY_DOMAIN>` | 推流域名，这里是 `<MACHINE_NAME>.<TAILNET_NAME>.ts.net` |
| `<APP_NAME>` | 应用名 / 部署代号（服务名 `fleet-telemetry-<APP_NAME>` 用） |
| `<SERVER_USER>` | 家里机器上的登录用户名（cron 里的脚本路径用） |

## templates/fleet_telemetry_config.json

| 占位符 | 含义 |
|---|---|
| `<VIN>` | 车架号，17 位 |
| `<TELEMETRY_DOMAIN>` | 推流域名 |
| `<TELEMETRY_FULLCHAIN_PEM>` | 推流域名证书全链 PEM 原文 |

## templates/telemetry-fields-180.json

无占位符，只有 180 个推流字段名与间隔秒数。

## templates/tesla_watcher.py

无占位符。阈值（低续航 62.14 英里 = 100 公里、停车 300 秒等）按 README 阶段 10 改。

## templates/bark_push.py

| 占位符 | 含义 |
|---|---|
| `<BARK_TITLE>` | Bark 通知标题 |
| `<AVATAR_URL>` | Bark 通知头像图片直链 |
| `<HOME_GCJ_LNG>` / `<HOME_GCJ_LAT>` | 家附近经纬度（GCJ-02） |
| `<HOME_AOI_NAME>` | 家附近高德 AOI 名字 |
| `<AMAP_KEY>` 所在文件路径 | 高德 Web 服务 Key |
| `<SERVER_USER>` | 服务器登录用户名 |
| 峰谷电价 `PEAK_PRICE` / `VALLEY_PRICE` | 示例电价，按当地实际电价改 |

## templates/push_channels.py

每个函数头部的注释已写明凭证含义，行尾注释也标明了每个凭证的含义。
凭证一览：`<BARK_DEVICE_KEY>`（Bark 设备 key）、`<TELEGRAM_BOT_TOKEN>`（Telegram 机器人 token）、`<TELEGRAM_CHAT_ID>`（Telegram 聊天 ID）、`<WECOM_WEBHOOK_KEY>`（企业微信群机器人 webhook key）、`<DINGTALK_ACCESS_TOKEN>`（钉钉 access_token）、`<DINGTALK_SECRET>`（钉钉加签 secret）、`<FEISHU_HOOK_ID>`（飞书 hook id）、`<FEISHU_SECRET>`（飞书签名密钥）、`<SERVERCHAN_SENDKEY>`（Server酱 SendKey）、`<PUSHPLUS_TOKEN>`（PushPlus token）、`<SMTP_HOST>` / `<SMTP_USER>` / `<SMTP_AUTH_CODE>` / `<NOTIFY_EMAIL>`（邮箱 SMTP 相关）、`<PUSHOVER_TOKEN>` / `<PUSHOVER_USER>`（Pushover）、`<PUSHDEER_KEY>`（PushDeer pushkey）、`<GOTIFY_URL>` / `<GOTIFY_TOKEN>`（自建 gotify）、`<NTFY_TOPIC>`（ntfy 话题名）。

## templates/telemetry_consumer.py

| 占位符 | 含义 |
|---|---|
| `UNIT_NAME` 里的服务名 | 自己的收流服务名 |

## templates/systemd/*.service

文件头注释已写明：`<SERVER_USER>` 是服务器登录用户名，`<APP_NAME>` 是应用名/部署代号。

## templates/credentials/bark_push.example.json

| 占位符 | 含义 |
|---|---|
| `<BARK_DEVICE_KEY>` | Bark 的设备 key |
