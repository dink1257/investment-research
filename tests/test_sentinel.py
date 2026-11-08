"""哨兵抓取合成测试（全部 mock，无网络，直接跑）。

覆盖：fetch_all 合并新闻+公告；非 A/港股返回 not_applicable；
run_sentinel 跳过 not_applicable 标的（不记失败、不告警）。
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import sentinel as sentinel_mod  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402

NOW = datetime(2026, 11, 9, 2, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _cfg():
    return {
        "thresholds": {"symbols": {
            "300750.SZ": {"name": "宁德时代", "unit": "pct", "regular": 4.72,
                          "emergency": 7.34},
            "AAPL": {"name": "苹果", "unit": "pct", "regular": 3.15,
                     "emergency": 4.92},
        }},
        "sentinel": {
            "industry_keywords": [],
            "grades": {"emergency": ["停牌"], "important": ["回购"],
                       "routine": []},
        },
        "budget": {"dedup_hours": 2},
        "push": {},
    }


def main():
    orig_news = sentinel_mod.fetch_akshare_news
    orig_notices = sentinel_mod.fetch_akshare_notices

    # 1. 非 A/港股 -> not_applicable，不调 AkShare
    items, status = sentinel_mod.fetch_all("AAPL", "20261109")
    assert items == [] and status == "not_applicable", (items, status)
    items, status = sentinel_mod.fetch_all("BTC-USD", "20261109")
    assert status == "not_applicable", status

    # 2. 新闻+公告合并，任一 ok 即 ok
    sentinel_mod.fetch_akshare_news = lambda code: (
        [{"title": "宁德时代 300750 公告：停牌核查", "source": "news",
          "content": "", "url": ""}], "ok")
    sentinel_mod.fetch_akshare_notices = lambda code, date: ([], "boom")
    try:
        items, status = sentinel_mod.fetch_all("300750.SZ", "20261109")
    finally:
        sentinel_mod.fetch_akshare_news = orig_news
        sentinel_mod.fetch_akshare_notices = orig_notices
    assert len(items) == 1 and status == "ok", (items, status)

    # 3. 两源都失败 -> 失败状态，不造假条目
    sentinel_mod.fetch_akshare_news = lambda code: ([], "unavailable: x")
    sentinel_mod.fetch_akshare_notices = lambda code, date: ([], "boom")
    try:
        items, status = sentinel_mod.fetch_all("300750.SZ", "20261109")
    finally:
        sentinel_mod.fetch_akshare_news = orig_news
        sentinel_mod.fetch_akshare_notices = orig_notices
    assert items == [] and "unavailable" in status and "boom" in status, \
        status

    # 4. run_sentinel 跳过 not_applicable：AAPL 不记失败、不告警
    alerts = []

    def recorder(config, title, content, dry_run=False, level="regular"):
        alerts.append(title)
        return {"channel": "test", "dry_run": True}

    def fetch_fn(symbol):
        if symbol == "AAPL":
            return [], "not_applicable"
        return ([{"title": "宁德时代 300750 公告：停牌核查",
                  "source": "test", "content": "", "url": ""}], "ok")

    conn = ledger_mod.init_db(":memory:")
    res = sentinel_mod.run_sentinel(conn, _cfg(), NOW, push_fn=recorder,
                                    fetch_fn=fetch_fn)
    assert res["counts"]["matched"] == 1, res
    assert "AAPL" not in res["sources"], res
    assert res["sources"].get("300750.SZ") == "ok", res
    # 连跑两天：AAPL 始终不触发数据源告警（300750 的紧急推送是预期的，
    # 每次间隔都超过 24 小时去重窗口）
    sentinel_mod.run_sentinel(conn, _cfg(), NOW + timedelta(days=1),
                              push_fn=recorder, fetch_fn=fetch_fn)
    sentinel_mod.run_sentinel(conn, _cfg(), NOW + timedelta(days=2),
                              push_fn=recorder, fetch_fn=fetch_fn)
    assert not any("数据源告警" in t for t in alerts), alerts
    assert all(t.startswith("哨兵紧急") for t in alerts), alerts

    print("test_sentinel: all passed")


if __name__ == "__main__":
    main()
