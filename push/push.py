"""推送层：分级推送（2026-10-30 用户定稿）。

- 紧急（emergency）：飞书 + Bark 同时发。
- 常规（regular）：只发飞书；飞书失败才走 Bark 补发；Bark 再失败才走
  Telegram（最后兜底）。成功即停，绝不把同一条原样发两遍。
- Bark 的 server/key 只放仓库 secrets（BARK_SERVER/BARK_KEY），
  由 workflow 注入运行期配置，绝不进仓库、绝不经手明文 key。

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
        body = r.read().decode("utf-8", errors="ignore")
    try:
        code = json.loads(body).get("code")
    except (ValueError, AttributeError):
        code = 0  # 非 JSON 响应：按 HTTP 成功处理
    if code != 0:
        # 飞书报错走 HTTP 200 + body code != 0；不确认送达就当失败，走补发。
        raise ValueError(f"飞书返回 code={code}：{body[:200]}")
    return {"channel": "feishu", "dry_run": False, "response": body}


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


def send_bark(server, key, title, content, dry_run=False):
    """Bark：POST {server}/{key}，JSON {"title": ..., "body": ...}。

    server 通常是 https://api.day.app（Bark App 里显示的地址为准）。
    """
    if dry_run or not server or not key:
        return {"channel": "bark", "dry_run": True,
                "payload": {"title": title, "body": content}}
    payload = {"title": title, "body": content}
    url = server.rstrip("/") + "/" + key
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = r.read().decode("utf-8", errors="ignore")
    try:
        code = json.loads(resp).get("code")
    except (ValueError, AttributeError):
        code = None
    if code != 200:
        raise ValueError(f"Bark 返回 code={code}：{resp[:200]}")
    return {"channel": "bark", "dry_run": False, "response": resp}


def send_alert(config, title, content, dry_run=False, level="regular"):
    """分级推送。level 为 "emergency" 或 "regular"（默认）。

    常规返回沿用旧形状 {"channel": ..., "dry_run": ...}，现有调用方
    （monitor 读 dry_run、smoke 断言 channel == "feishu"）不受影响；
    紧急返回 {"channel": "feishu+bark", "dry_run": ..., "results": [...]}，
    任一通道失败记入 errors；两通道都失败才走 Telegram 兜底。
    所有返回都带 "delivered"：至少一个通道真实发出才为 True，
    dry_run 或凭证为空的占位返回均为 False（monitor 与 tools/test_push.py
    靠它记账和判定测试成败）。
    Bark 无凭证时不伪造成功：常规跳过 Bark 直接走 Telegram，
    紧急只返回实际发出的通道。
    """
    push = config.get("push", {})
    server = push.get("bark_server", "")
    key = push.get("bark_key", "")
    if level == "emergency":
        results, errors = [], []
        try:
            results.append(send_feishu(push.get("feishu_webhook", ""),
                                       title, content, dry_run=dry_run))
        except Exception as e:  # noqa: BLE001
            errors.append(f"feishu: {e}")
        if dry_run or (server and key):
            try:
                results.append(send_bark(server, key, title, content,
                                         dry_run=dry_run))
            except Exception as e:  # noqa: BLE001
                errors.append(f"bark: {e}")
        if results:
            return {"channel": "+".join(r["channel"] for r in results),
                    "dry_run": bool(dry_run),
                    "delivered": any(not r.get("dry_run", True)
                                     for r in results),
                    "results": results,
                    "errors": errors}
        res = send_telegram(push.get("telegram_bot_token", ""),
                            push.get("telegram_chat_id", ""),
                            f"{title}\n{content}\n（飞书与 Bark 均失败："
                            f"{'; '.join(errors)}）",
                            dry_run=dry_run)
        res["delivered"] = not res.get("dry_run", True)
        return res
    try:
        res = send_feishu(push.get("feishu_webhook", ""), title, content,
                          dry_run=dry_run)
        res["delivered"] = not res.get("dry_run", True)
        return res
    except Exception as e:  # noqa: BLE001
        if server and key:
            try:
                res = send_bark(server, key, title, content,
                                dry_run=dry_run)
                res["delivered"] = not res.get("dry_run", True)
                return res
            except Exception:  # noqa: BLE001
                pass
        res = send_telegram(push.get("telegram_bot_token", ""),
                            push.get("telegram_chat_id", ""),
                            f"{title}\n{content}\n（飞书主通道失败：{e}）",
                            dry_run=dry_run)
        res["delivered"] = not res.get("dry_run", True)
        return res
