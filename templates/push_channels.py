#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 推送渠道模块：12 种常见推送渠道，每种一个函数，签名统一为 send_xxx(title, body)。
# 用法：在推送脚本里按需 import 并调用；凭证一律从环境变量或 600 权限的配置文件读，
# 不要把真实 key/token 写进代码，更不要提交进公开仓库。
# 占位符说明见 README 第 2 节《占位符表》，全文统一 <全大写下划线> 格式。
# 每个函数只依赖 Python 标准库，可直接复制使用。

import base64
import hashlib
import hmac
import json
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText


def _post_json(url, payload, timeout=15):
    """POST 一个 JSON，返回解析后的 dict；失败抛异常，由调用方决定是否重试。"""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_form(url, fields, timeout=15):
    """POST 一组表单字段，返回解析后的 dict。"""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- Bark（iOS）
# 申请：iPhone 装 Bark App → 打开即得 device_key（形如一串字母数字）。
# 凭证：BARK_DEVICE_KEY。服务端：https://api.day.app/push（官方免费服务）。
def send_bark(title, body, level="active"):
    # level：active（普通）/ timeSensitive（锁屏强提醒，启动、超速、急刹用这个）
    return _post_json("https://api.day.app/push", {
        "device_key": "<BARK_DEVICE_KEY>",  #  Bark 的设备 key
        "title": title, "body": body,
        "level": level, "icon": "<AVATAR_URL>",  #  Bark 通知头像图片直链
    })


# ------------------------------------------------------- Telegram Bot（需翻墙）
# 申请：Telegram 里找 @BotFather → /newbot → 得 token；再给机器人发一条消息，
# 用 https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates 查 chat_id。
# 注意：中国大陆服务器直连 api.telegram.org 不通，须走代理（见 README 阶段 11）。
def send_telegram(title, body):
    return _post_json(
        "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/sendMessage",  #  Telegram 机器人 token
        {"chat_id": "<TELEGRAM_CHAT_ID>",  #  Telegram 聊天 ID
         "text": f"{title}\n{body}"})


# ------------------------------------------------- 企业微信群机器人（免费）
# 申请：企业微信群 → 右上角 … → 添加群机器人 → 复制 webhook 地址里的 key。
# 只能发到群里，不能私聊个人。
def send_wecom(title, body):
    return _post_json(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<WECOM_WEBHOOK_KEY>",  # 企业微信群机器人 webhook key
        {"msgtype": "markdown",
         "markdown": {"content": f"**{title}**\n{body}"}})


# ----------------------------------------------------- 钉钉群机器人（免费）
# 申请：钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 选“自定义” → 复制 webhook 的
# access_token；如选“加签”，把 secret 一并记下（不加签也能用，但建议加签）。
def send_dingtalk(title, body):
    url = "https://oapi.dingtalk.com/robot/send?access_token=<DINGTALK_ACCESS_TOKEN>"  # 钉钉群机器人 access_token
    secret = "<DINGTALK_SECRET>"  # 钉钉加签 secret；没选加签就把下面三行删掉，只用上面的 url
    if secret and not secret.startswith("<"):
        ts = str(round(time.time() * 1000))
        sign = urllib.parse.quote_plus(base64.b64encode(hmac.new(
            secret.encode("utf-8"), f"{ts}\n{secret}".encode("utf-8"),
            digestmod=hashlib.sha256).digest()).decode("utf-8"))
        url += f"&timestamp={ts}&sign={sign}"
    return _post_json(url, {"msgtype": "text",
                            "text": {"content": f"{title}\n{body}"}})


# ----------------------------------------------------- 飞书群机器人（免费）
# 申请：飞书群 → 设置 → 群机器人 → 添加机器人 → 复制 webhook 地址最后一段 hook id；
# 如开启签名校验，把密钥记下。
def send_feishu(title, body):
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/<FEISHU_HOOK_ID>"  # 飞书群机器人 hook id
    secret = "<FEISHU_SECRET>"  # 飞书签名密钥；没开签名校验就删掉下面三行
    if secret and not secret.startswith("<"):
        ts = str(int(time.time()))
        sign = base64.b64encode(hmac.new(
            secret.encode("utf-8"), f"{ts}\n{secret}".encode("utf-8"),
            digestmod=hashlib.sha256).digest()).decode("utf-8")
        payload = {"timestamp": ts, "sign": sign,
                   "msg_type": "text", "content": {"text": f"{title}\n{body}"}}
        return _post_json(url, payload)
    return _post_json(url, {"msg_type": "text",
                            "content": {"text": f"{title}\n{body}"}})


# ---------------------------------------------------------- Server酱（免费版限量）
# 申请：sct.ftqq.com 微信扫码登录 → SendKey → 复制。
# 免费版每天 5 条，超了不发也不报错，提醒多的人别用这个做主渠道。
def send_serverchan(title, body):
    return _post_form(
        "https://sctapi.ftqq.com/<SERVERCHAN_SENDKEY>.send",  #  Server酱 SendKey
        {"title": title, "desp": body})


# ------------------------------------------------- PushPlus（推到微信，免费版限量）
# 申请：pushplus.plus 微信扫码登录 → 复制 token。
# channel=wechat 时推到微信服务号消息里，手机微信直接收到。
def send_pushplus(title, body):
    return _post_form("http://www.pushplus.plus/send", {
        "token": "<PUSHPLUS_TOKEN>",  #  PushPlus token
        "title": title,
        "content": body, "template": "txt", "channel": "wechat"})


# ------------------------------------------------------------- 邮件 SMTP（免费）
# 申请：邮箱开 SMTP 服务并拿到授权码（不是登录密码）。
# QQ 邮箱：设置 → 账户 → 开启 IMAP/SMTP → 授权码；163 同理；Gmail 用应用专用密码。
# iOS 自带邮件 App 收信不及时，适合做备份渠道，不适合做主提醒。
# QQ/163 邮箱 SMTP 端口示例：465（SSL）或 587（TLS），按邮箱要求改
SMTP_PORT_EXAMPLE = 465


def send_email(title, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = title
    msg["From"] = "<SMTP_USER>"  # 发件邮箱地址
    msg["To"] = "<NOTIFY_EMAIL>"  # 收通知的邮箱
    with smtplib.SMTP_SSL("<SMTP_HOST>", SMTP_PORT_EXAMPLE) as s:  # 邮箱 SMTP 服务器地址
        s.login("<SMTP_USER>", "<SMTP_AUTH_CODE>")  # 邮箱 SMTP 授权码（不是登录密码）
        s.send_message(msg)
    return {"ok": True}


# ------------------------------------------------------------- Pushover（付费）
# 申请：pushover.net 注册 → Create an Application → 得 token；User Key 在账号页。
# 一次性付费约 5 美元，iOS 推送很稳；大陆服务器直连可能慢，超时就重试一次。
def send_pushover(title, body, priority=0):
    return _post_form("https://api.pushover.net/1/messages.json", {
        "token": "<PUSHOVER_TOKEN>",  #  Pushover 应用 token
        "user": "<PUSHOVER_USER>",  #  Pushover 用户 key
        "title": title, "message": body, "priority": priority})


# -------------------------------------------------------------- PushDeer（免费）
# 申请：pushdeer.com 微信扫码登录 → 复制 pushkey；手机装 PushDeer App 扫码绑定。
# 国内服务，不用翻墙；免费版每分钟限 10 条。
def send_pushdeer(title, body):
    url = ("https://api2.pushdeer.com/message/push"
           f"?pushkey=<PUSHDEER_KEY>&text={urllib.parse.quote(title)}"  #  PushDeer pushkey
           f"&desp={urllib.parse.quote(body)}")
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


# ------------------------------------------------- gotify（自建，免费，需自己搭服务）
# 申请：自己服务器上装 gotify/server → 建一个应用得 token。
# 全程走自己的服务器，数据不经过第三方；缺点是手机端要装 gotify App 且保持后台。
def send_gotify(title, body, priority=5):
    return _post_json(
        "<GOTIFY_URL>/message?token=<GOTIFY_TOKEN>",  # 自建 gotify 服务地址 + 应用 token
        {"title": title, "message": body, "priority": priority})


# ------------------------------------------- ntfy（免费，可用公共服务或自建）
# 申请：不用申请，话题名就是地址。手机装 ntfy App → 订阅同一个话题即可收到。
# 公共服务 ntfy.sh 免费但话题名公开可猜，别用车牌、姓名这类当话题名。
def send_ntfy(title, body, priority="default"):
    req = urllib.request.Request(
        "https://ntfy.sh/<NTFY_TOPIC>",  #  ntfy 话题名
        data=body.encode("utf-8"), method="POST",
        headers={"Title": title, "Priority": priority})
    with urllib.request.urlopen(req, timeout=15) as r:
        return {"ok": r.status == 200}


# ---------------------------------------------------------------- 接入示例
# 在推送脚本（templates/bark_push.py 的 process_once）里，拿到 text 后按需调用：
#
#   from push_channels import send_bark, send_wecom, send_telegram
#   send_bark("<BARK_TITLE>", text, level)          # 主渠道：Bark
#   send_wecom("<BARK_TITLE>", text)                # 备用渠道：企业微信群
#   # send_telegram("<BARK_TITLE>", text)           # 需要翻墙，默认注释掉
#
# 建议：主渠道 1 个 + 备用渠道 1 个就够了，渠道越多，漏推、重复推的坑越多。
# 所有渠道共用同一份 text（README 阶段 12 的文案模板），不用每个渠道各写一套。
