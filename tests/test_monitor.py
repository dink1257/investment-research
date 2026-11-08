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

    # 辩论驱动默认桩：拦截，不调真实 LLM（各用例按需改桩）。
    mon.debate_mod.run_debate = lambda *a, **k: {"blocked": "test_no_llm"}

    # ---- 常规触发 -> dry-run 预警 + 快照 + 心跳 ----
    mon.fetch_symbol = lambda s, proxies=None: (
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
        mon.fetch_symbol = lambda s, proxies=None: (
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
    mon.fetch_symbol = lambda s, proxies=None: (
        {"symbol": s, "price": 110.0, "prev_close": 100.0, "pct": 10.0,
         "source": "yahoo"}, True, False)
    conn3 = ledger_mod.init_db(":memory:")
    summary4 = mon.run_cycle(make_config(hb), conn3, ["BTC-USD"], dry_run=True)
    assert summary4["disagreements"] == ["BTC-USD"], summary4
    assert summary4["dry_run_alerts"] == 0, summary4
    snap = ledger_mod.get_recent_snapshots(conn3)[0]
    assert snap[4] == "disagreement", snap

    # ---- 行情全失败 -> 计 error，不写快照 ----
    mon.fetch_symbol = lambda s, proxies=None: (None, False, False)
    conn4 = ledger_mod.init_db(":memory:")
    summary5 = mon.run_cycle(make_config(hb), conn4, ["BTC-USD"], dry_run=True)
    assert summary5["errors"] == ["BTC-USD: 行情全失败"], summary5
    assert summary5["snapshots"] == 0, summary5

    # ---- 辩论驱动：结论推送（dry-run）----
    verdict = {"verdict": "短期超跌，可分批关注", "target_range": "300-330",
               "position": "轻仓试探", "stop_loss": "跌破 290 离场",
               "confidence": 4, "uncertain": ["财报后指引未明"]}
    mon.fetch_symbol = lambda s, proxies=None: (
        {"symbol": s, "price": 105.0, "prev_close": 100.0, "pct": 5.0,
         "source": "yahoo"}, False, False)
    mon.debate_mod.run_debate = lambda *a, **k: {
        "verdict": verdict, "cost_rmb": 0.07, "blocked": None}
    conn5 = ledger_mod.init_db(":memory:")
    summary6 = mon.run_cycle(make_config(hb), conn5, ["BTC-USD"],
                             dry_run=True)
    assert summary6["debates_run"] == 1, summary6
    assert summary6["verdicts_dry_run"] == 1, summary6
    assert summary6["debates_blocked"] == {}, summary6
    rows5 = conn5.execute(
        "SELECT title, dry_run FROM alerts ORDER BY rowid").fetchall()
    assert len(rows5) == 2, rows5  # 阈值告警 + 辩论结论
    assert rows5[1][0].startswith("辩论研判：比特币"), rows5[1]
    assert rows5[1][1] == 1, rows5[1]
    text = mon._format_verdict("比特币", "BTC-USD", "regular",
                               "+5.00%（常规 ±4.5% / 紧急 ±6.6%）", verdict)
    assert "信心 4/5" in text and "不确定项" in text, text

    # ---- 辩论驱动：预算拦截 -> 只计 blocked，不推结论 ----
    mon.debate_mod.run_debate = lambda *a, **k: {
        "blocked": "daily_debate_cap"}
    conn6 = ledger_mod.init_db(":memory:")
    summary7 = mon.run_cycle(make_config(hb), conn6, ["BTC-USD"],
                             dry_run=True)
    assert summary7["debates_run"] == 0, summary7
    assert summary7["debates_blocked"] == {"daily_debate_cap": 1}, summary7
    assert summary7["verdicts_dry_run"] == 0, summary7
    assert conn6.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1

    # ---- 辩论驱动：解析失败 -> 计 error，心跳变 partial ----
    mon.debate_mod.run_debate = lambda *a, **k: {
        "error": "parse_failed", "detail": "bad json", "cost_rmb": 0.07}
    conn7 = ledger_mod.init_db(":memory:")
    summary8 = mon.run_cycle(make_config(hb), conn7, ["BTC-USD"],
                             dry_run=True)
    assert summary8["debates_errored"] == 1, summary8
    assert any("辩论失败" in e for e in summary8["errors"]), summary8
    with open(hb, encoding="utf-8") as f:
        assert json.load(f)["status"] == "partial"

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
