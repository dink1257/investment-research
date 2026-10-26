"""推送层：飞书主通道 + Telegram 备通道（failover，不重复投递）。

dry_run 或凭证为空时只返回 payload，不发真实请求——测试与影子运行用 dry_run。
"""
import json
import urllib.request


def send_feishu(webhook_url, title, content, dry_run=False):
    payload = {"msg_type": "text",
               "content": {"text": f"{title}\n{content}"}}
    if dry_run or not webhook_url:
        return {"channel": "feishu", "dry_run": True, "payload": payload}
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return {"channel": "feishu", "dry_run": False,
                "response": r.read().decode("utf-8", errors="ignore")}


def send_telegram(bot_token, chat_id, text, dry_run=False):
    if dry_run or not bot_token or not chat_id:
        return {"channel": "telegram", "dry_run": True,
                "payload": {"chat_id": chat_id, "text": text}}
    payload = {"chat_id": chat_id, "text": text}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return {"channel": "telegram", "dry_run": False,
                "response": r.read().decode("utf-8", errors="ignore")}


def send_alert(config, title, content, dry_run=False):
    """主备 failover：飞书失败才走 Telegram；主通道成功绝不重复投递。"""
    push = config.get("push", {})
    try:
        return send_feishu(push.get("feishu_webhook", ""), title, content,
                           dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        return send_telegram(push.get("telegram_bot_token", ""),
                             push.get("telegram_chat_id", ""),
                             f"{title}\n{content}\n（飞书主通道失败：{e}）",
                             dry_run=dry_run)
