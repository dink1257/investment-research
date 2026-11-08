"""监控轮询测试（行情函数全部 mock，无网络，直接跑）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.monitor as mon  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402


def make_config(hb_path):
    return {
        "thresholds": {"symbols": {
            "BTC-USD": {"name": "比特币", "unit": "pct", "regular": 4.5,
                        "emergency": 6.6, "sigma": 2.0},
            "^TNX": {"name": "10年美债收益率", "unit": "bp",
                     "regular_bp": 10, "emergency_bp": 20},
        }},
        "budget": {"dedup_hours": 2},
        "gate_chain": {"order": ["enabled", "not_expired", "trading_session",
                                 "daily_limit", "cooldown", "dedup",
                                 "quiet_hours"]},
        "monitor": {"heartbeat_file": hb_path},
        "push": {},
    }


def main():
    tmp = tempfile.mkdtemp()
    hb = os.path.join(tmp, "heartbeat.json")

    # ---- 常规触发 -> dry-run 预警 + 快照 + 心跳 ----
    mon.fetch_symbol = lambda s: (
        {"symbol": s, "price": 105.0, "prev_close": 100.0, "pct": 5.0,
         "source": "yahoo"}, False, False) if s == "BTC-USD" else (None, False,
                                                               False)
    conn = ledger_mod.init_db(":memory:")
    summary = mon.run_cycle(make_config(hb), conn, ["BTC-USD"], dry_run=True)
    assert summary["triggers"] == 1, summary
    assert summary["dry_run_alerts"] == 1, summary
    assert summary["snapshots"] == 1, summary
    assert summary["alerts_sent"] == 0, summary
    rows = conn.execute("SELECT symbol, level, pushed, dry_run FROM alerts"
                        ).fetchall()
    assert len(rows) == 1 and rows[0][1] == "regular" and rows[0][3] == 1, rows
    with open(hb, encoding="utf-8") as f:
        hb_data = json.load(f)
    assert hb_data["status"] == "ok" and hb_data["checked"] == 1, hb_data
    snaps = ledger_mod.get_recent_snapshots(conn)
    assert len(snaps) == 1 and snaps[0][1] == "BTC-USD", snaps

    # ---- 立即再跑一轮 -> 门控拦截（冷却），不重复预警 ----
    summary2 = mon.run_cycle(make_config(hb), conn, ["BTC-USD"], dry_run=True)
    assert summary2["triggers"] == 1, summary2
    assert summary2["dry_run_alerts"] == 0, summary2
    assert summary2["blocked"] == {"cooldown": 1}, summary2
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1

    # ---- 基点型标的（^TNX +15bp -> 常规档）----
    # ^TNX 是期货/外汇类，只在工作日放行；测试不依赖真实日期，强制放行
    # 交易时段门（否则周末跑测试会失败）。
    _orig_session = mon.gates.in_trading_session
    mon.gates.in_trading_session = lambda *a, **k: True
    try:
        mon.fetch_symbol = lambda s: (
            {"symbol": s, "price": 5.15, "prev_close": 5.00, "pct": 3.0,
             "source": "yahoo"}, False, False)
        conn2 = ledger_mod.init_db(":memory:")
        summary3 = mon.run_cycle(make_config(hb), conn2, ["^TNX"], dry_run=True)
        assert summary3["dry_run_alerts"] == 1, summary3
        row = conn2.execute("SELECT level FROM alerts").fetchone()
        assert row[0] == "regular", row
    finally:
        mon.gates.in_trading_session = _orig_session

    # ---- 双源不一致 -> 只记账本（disagreement），不推送 ----
    mon.fetch_symbol = lambda s: (
        {"symbol": s, "price": 110.0, "prev_close": 100.0, "pct": 10.0,
         "source": "yahoo"}, True, False)
    conn3 = ledger_mod.init_db(":memory:")
    summary4 = mon.run_cycle(make_config(hb), conn3, ["BTC-USD"], dry_run=True)
    assert summary4["disagreements"] == ["BTC-USD"], summary4
    assert summary4["dry_run_alerts"] == 0, summary4
    snap = ledger_mod.get_recent_snapshots(conn3)[0]
    assert snap[4] == "disagreement", snap

    # ---- 行情全失败 -> 计 error，不写快照 ----
    mon.fetch_symbol = lambda s: (None, False, False)
    conn4 = ledger_mod.init_db(":memory:")
    summary5 = mon.run_cycle(make_config(hb), conn4, ["BTC-USD"], dry_run=True)
    assert summary5["errors"] == ["BTC-USD: 行情全失败"], summary5
    assert summary5["snapshots"] == 0, summary5

    # ---- 退出码方案 C（只在全部失败时返回非零）----
    def _s(**kw):
        base = {"checked": 1, "snapshots": 1, "triggers": 0,
                "alerts_sent": 0, "dry_run_alerts": 0, "blocked": {}}
        base.update(kw)
        return base
    assert mon.is_total_failure(_s(checked=1, snapshots=0)) is True  # 行情全失败
    assert mon.is_total_failure(_s()) is False  # 正常一轮
    assert mon.is_total_failure(_s(triggers=1, blocked={"cooldown": 1})) \
        is False  # 触发被门控拦住，不算推送失败
    assert mon.is_total_failure(_s(triggers=1)) is True  # 放行但一条没送达
    assert mon.is_total_failure(_s(triggers=1, dry_run_alerts=1)) \
        is False  # dry-run 不算推送失败
    assert mon.is_total_failure(_s(triggers=2, alerts_sent=1)) \
        is False  # 部分送达不算全部失败
    assert mon.is_total_failure(_s(checked=0, snapshots=0)) is False  # 空表
    print("test_monitor: all passed")


if __name__ == "__main__":
    main()
