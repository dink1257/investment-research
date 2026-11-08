"""全流程端到端测试（方案 §6 集成测试：监控→触发→辩论→账本→推送）。

历史数据回放形态：行情与 LLM 全部 mock（录制 fixture），不调真实 LLM、
不联网。断言整条链：快照、触发、辩论、账本、推送 dry-run、心跳。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import llm as llm_mod  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402
import services.monitor as mon  # noqa: E402
from tools.run_deepdive import render_markdown  # noqa: E402


def make_config(hb_path):
    return {
        "thresholds": {"symbols": {
            "300750.SZ": {"name": "宁德时代", "unit": "pct", "regular": 4.72,
                          "emergency": 7.34, "sigma": 2.0},
            "600519.SS": {"name": "贵州茅台", "unit": "pct", "regular": 3.0,
                          "emergency": 3.86, "sigma": 1.4},
        }},
        "budget": {"monthly_cap": 20, "warn_at": 16, "daily_debate_cap": 8,
                   "daily_deepdive_cap": 2, "dedup_hours": 2},
        "gate_chain": {"order": ["enabled", "not_expired", "trading_session",
                                 "daily_limit", "cooldown", "dedup",
                                 "quiet_hours"]},
        "monitor": {"heartbeat_file": hb_path},
        "push": {},
    }


DEBATE_OK = json.dumps({
    "bull": "多方：需求回暖", "bear": "空方：竞争加剧",
    "bull_rebuttal": "反驳1", "bear_rebuttal": "反驳2",
    "verdict": "短期超跌，可分批关注", "target_range": "200-220",
    "position": "轻仓", "stop_loss": "跌破 190 作废", "confidence": 4,
    "uncertain": ["需核实 Q3 指引"]}, ensure_ascii=False)


class MockLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def __call__(self, system, user, max_tokens):
        self.calls += 1
        text = self.outputs.pop(0) if self.outputs else "{}"
        return text, {}, 0.01


def main():
    tmp = tempfile.mkdtemp()
    hb = os.path.join(tmp, "heartbeat.json")
    cfg = make_config(hb)

    # 行情桩：宁德 +5.0%（触发常规档），茅台 +0.5%（不触发）
    def fetch_stub(symbol, proxies=None):
        if symbol == "300750.SZ":
            return ({"symbol": symbol, "price": 262.5, "prev_close": 250.0,
                     "pct": 5.0, "source": "yahoo"}, False, False)
        return ({"symbol": symbol, "price": 1700.0, "prev_close": 1690.0,
                 "pct": 0.59, "source": "yahoo"}, False, False)

    mon.fetch_symbol = fetch_stub
    # 交易时段门强制放行（测试不依赖真实时间）
    orig_session = mon.gates.in_trading_session
    mon.gates.in_trading_session = lambda *a, **k: True
    orig_call = llm_mod.call_qwen
    llm_mod.call_qwen = MockLLM([DEBATE_OK])
    try:
        conn = ledger_mod.init_db(":memory:")
        summary = mon.run_cycle(cfg, conn, ["300750.SZ", "600519.SS"],
                                dry_run=True)
    finally:
        mon.gates.in_trading_session = orig_session
        llm_mod.call_qwen = orig_call
    assert summary["checked"] == 2, summary
    assert summary["snapshots"] == 2, summary
    assert summary["triggers"] == 1, summary
    assert summary["debates_run"] == 1, summary
    assert summary["verdicts_dry_run"] == 1, summary
    assert summary["dry_run_alerts"] == 1, summary
    assert summary["errors"] == [], summary
    assert summary["debates_blocked"] == {}, summary
    with open(hb, encoding="utf-8") as f:
        assert json.load(f)["status"] == "ok"
    # 账本：1 条辩论研判 + 2 条推送记录（阈值告警 + 辩论结论）
    rows = conn.execute(
        "SELECT kind, symbol FROM judgments").fetchall()
    assert len(rows) == 1 and rows[0] == ("debate", "300750.SZ"), rows
    alerts = conn.execute(
        "SELECT title, dry_run FROM alerts ORDER BY rowid").fetchall()
    assert len(alerts) == 2, alerts
    assert alerts[0][0].startswith("异动提醒：宁德时代"), alerts
    assert alerts[1][0].startswith("辩论研判：宁德时代"), alerts
    assert all(r[1] == 1 for r in alerts), alerts

    # 第二轮立即再跑：冷却拦住阈值告警，辩论也不跑（门控在辩论之前）
    mon.fetch_symbol = fetch_stub
    mon.gates.in_trading_session = lambda *a, **k: True
    llm_mod.call_qwen = MockLLM([DEBATE_OK])
    try:
        summary2 = mon.run_cycle(cfg, conn, ["300750.SZ", "600519.SS"],
                                 dry_run=True)
    finally:
        mon.gates.in_trading_session = orig_session
        llm_mod.call_qwen = orig_call
    assert summary2["triggers"] == 1, summary2
    assert summary2["blocked"] == {"cooldown": 1}, summary2
    assert summary2["debates_run"] == 0, summary2

    # 深研报告渲染（CLI 的 Markdown 输出）
    report = {"verdict": "结论", "fundamental": "基本面",
              "technical": "技术面", "capital": "资金面",
              "risks": ["风险1"], "buy_point": "110",
              "sell_point": "130", "risk_strategy": "轻仓+止损",
              "uncertain": ["待核实"], "backtest": {"hit": True}}
    md = render_markdown("300750.SZ", "宁德时代", report, 0.13)
    assert "# 深研报告：宁德时代（300750.SZ）" in md, md
    assert "买点：110" in md and "风险策略" in md, md
    assert "¥0.13" in md, md

    print("test_e2e: all passed")


if __name__ == "__main__":
    main()
