"""Bark 分级推送测试：dry-run 与 monkeypatch，不发真实请求。

覆盖 2026-10-30 定稿的分级：紧急飞书+Bark，常规只飞书、
飞书失败才 Bark、 Bark 失败才 Telegram；delivered 只在真实发出时为 True。
用法：python3 tests/test_bark_push.py（也可 pytest）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from push import push as push_mod  # noqa: E402


def _cfg(with_bark=True):
    cfg = {"push": {"feishu_webhook": "https://example.invalid/hook",
                    "telegram_bot_token": "tok",
                    "telegram_chat_id": "1"}}
    if with_bark:
        cfg["push"]["bark_server"] = "https://api.day.app"
        cfg["push"]["bark_key"] = "test-key"
    return cfg


def _live(channel):
    return {"channel": channel, "dry_run": False, "response": "ok"}


def test_regular_dry_run_feishu_only():
    res = push_mod.send_alert(_cfg(), "标题", "内容", dry_run=True)
    assert res["channel"] == "feishu", res
    assert res["dry_run"] is True and res["delivered"] is False, res


def test_emergency_dry_run_both():
    res = push_mod.send_alert(_cfg(), "标题", "内容", dry_run=True,
                              level="emergency")
    assert res["channel"] == "feishu+bark", res
    assert [r["channel"] for r in res["results"]] == ["feishu", "bark"], res
    assert res["delivered"] is False and res["errors"] == [], res


def test_regular_feishu_fail_falls_back_to_bark():
    cfg = _cfg()
    orig_feishu = push_mod.send_feishu
    orig_bark = push_mod.send_bark
    push_mod.send_feishu = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("feishu down"))
    push_mod.send_bark = lambda *a, **k: _live("bark")
    try:
        res = push_mod.send_alert(cfg, "标题", "内容", dry_run=False)
    finally:
        push_mod.send_feishu = orig_feishu
        push_mod.send_bark = orig_bark
    assert res["channel"] == "bark" and res["delivered"] is True, res


def test_regular_no_bark_creds_falls_back_to_telegram():
    cfg = _cfg(with_bark=False)
    orig_feishu = push_mod.send_feishu
    orig_tg = push_mod.send_telegram
    push_mod.send_feishu = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("feishu down"))
    push_mod.send_telegram = lambda *a, **k: _live("telegram")
    try:
        res = push_mod.send_alert(cfg, "标题", "内容", dry_run=False)
    finally:
        push_mod.send_feishu = orig_feishu
        push_mod.send_telegram = orig_tg
    assert res["channel"] == "telegram" and res["delivered"] is True, res


def test_emergency_feishu_fail_still_sends_bark():
    cfg = _cfg()
    orig_feishu = push_mod.send_feishu
    orig_bark = push_mod.send_bark
    push_mod.send_feishu = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("feishu down"))
    push_mod.send_bark = lambda *a, **k: _live("bark")
    try:
        res = push_mod.send_alert(cfg, "标题", "内容", dry_run=False,
                                  level="emergency")
    finally:
        push_mod.send_feishu = orig_feishu
        push_mod.send_bark = orig_bark
    assert res["channel"] == "bark" and res["delivered"] is True, res
    assert any("feishu" in e for e in res["errors"]), res


def test_send_bark_payload_shape():
    res = push_mod.send_bark("https://api.day.app", "k", "标题", "内容",
                             dry_run=True)
    assert res["channel"] == "bark" and res["dry_run"] is True, res
    assert res["payload"] == {"title": "标题", "body": "内容"}, res


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name}: OK")
    print("test_bark_push: all passed")
