"""Week 2 测试（预算、辩论、深研回测、哨兵、集成链路；LLM 全部 mock，无网络）。"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import debate as debate_mod  # noqa: E402
from agents import deepdive as deepdive_mod  # noqa: E402
from agents import llm as llm_mod  # noqa: E402
from core import backtest as backtest_mod  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402
from push import push as push_mod  # noqa: E402
from services import sentinel as sentinel_mod  # noqa: E402

BJ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 27, 20, 0, tzinfo=BJ)


def make_config():
    return {
        "thresholds": {"symbols": {
            "300750.SZ": {"name": "宁德时代", "unit": "pct", "regular": 4.72,
                         "emergency": 7.34, "sigma": 2.0},
            "600519.SS": {"name": "贵州茅台", "unit": "pct", "regular": 3.0,
                         "emergency": 3.86, "sigma": 1.4},
        }},
        "budget": {"monthly_cap": 20, "warn_at": 16, "daily_debate_cap": 8,
                   "daily_deepdive_cap": 2, "dedup_hours": 2},
        "sentinel": {
            "industry_keywords": [
                {"group": "锂电与新能源",
                 "keywords": ["固态电池", "动力电池", "储能"]}],
            "grades": {"emergency": ["停牌", "立案调查", "业绩暴雷", "重大重组",
                                     "实控人变更"],
                       "important": ["财报", "业绩预告", "减持", "增持", "回购",
                                     "问询函"],
                       "routine": ["日常公告", "调研纪要"]},
        },
        "gate_chain": {"order": []},
        "monitor": {"heartbeat_file": "/tmp/never.json"},
        "push": {},
    }


class MockLLM:
    """按顺序返回 canned 输出；记录调用次数。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def __call__(self, system, user, max_tokens):
        self.calls += 1
        text = self.outputs.pop(0) if self.outputs else "{}"
        return text, {}, 0.01


DEBATE_OK = json.dumps({
    "bull": "多方", "bear": "空方", "bull_rebuttal": "反驳1",
    "bear_rebuttal": "反驳2", "verdict": "看多：理由",
    "target_range": "200-220", "position": "轻仓",
    "stop_loss": "跌破 190 作废", "confidence": 4,
    "uncertain": ["需核实事项"]}, ensure_ascii=False)
DEBATE_LOW = json.dumps({
    "bull": "多方", "bear": "空方", "bull_rebuttal": "反驳1",
    "bear_rebuttal": "反驳2", "verdict": "看多：理由",
    "target_range": None, "position": "轻仓",
    "stop_loss": "跌破 190 作废", "confidence": 2,
    "uncertain": ["需核实事项"]}, ensure_ascii=False)
DEEPDIVE_OK = json.dumps({
    "fundamental": "基本面", "technical": "技术面", "capital": "资金面",
    "risks": ["风险1", "风险2", "风险3"], "buy_point": "110",
    "sell_point": "130", "risk_strategy": "轻仓+止损",
    "verdict": "结论", "uncertain": []}, ensure_ascii=False)


def add_judgment(conn, kind, symbol, ts):
    ledger_mod.record_judgment(conn, ts, kind, symbol, "t", "{}", 0.0)


def main():
    cfg = make_config()

    # ---- 预算：日配额 ----
    conn = ledger_mod.init_db(":memory:")
    for _ in range(8):
        add_judgment(conn, "debate", "300750.SZ", NOW.isoformat())
    mock = MockLLM([DEBATE_OK])
    res = debate_mod.run_debate(conn, cfg, "300750.SZ", "宁德时代",
                                "+5.0%", "regular", 5.0, now=NOW,
                                llm_call=mock)
    assert res["blocked"] == "daily_debate_cap" and mock.calls == 0, res

    # ---- 预算：月熔断 ¥16（常规档拦、紧急大档放、深研停）----
    conn = ledger_mod.init_db(":memory:")
    ledger_mod.add_cost(conn, "2026-10-01", "qwen3.8-flash", 1, 16.5)
    mock = MockLLM([DEBATE_OK])
    res = debate_mod.run_debate(conn, cfg, "300750.SZ", "宁德时代",
                                "+5.0%", "regular", 5.0, now=NOW,
                                llm_call=mock)
    assert res["blocked"] == "monthly_warn_16" and mock.calls == 0, res
    mock = MockLLM([DEBATE_OK])
    res = debate_mod.run_debate(conn, cfg, "300750.SZ", "宁德时代",
                                "+8.0%", "emergency", 8.0, now=NOW,
                                llm_call=mock)
    assert res.get("blocked") is None and mock.calls == 1, res
    mock = MockLLM([DEEPDIVE_OK])
    res = deepdive_mod.run_deepdive(conn, cfg, "300750.SZ", "宁德时代",
                                    "简报", now=NOW, llm_call=mock,
                                    closes=[100.0] * 60)
    assert res["blocked"] == "monthly_warn_16" and mock.calls == 0, res

    # ---- 预算：月熔断 ¥20（全停）----
    conn = ledger_mod.init_db(":memory:")
    ledger_mod.add_cost(conn, "2026-10-01", "qwen3.8-flash", 1, 20.0)
    mock = MockLLM([DEBATE_OK])
    res = debate_mod.run_debate(conn, cfg, "300750.SZ", "宁德时代",
                                "+8.0%", "emergency", 8.0, now=NOW,
                                llm_call=mock)
    assert res["blocked"] == "monthly_cap_20" and mock.calls == 0, res

    # ---- 辩论 2 小时去重 ----
    conn = ledger_mod.init_db(":memory:")
    add_judgment(conn, "debate", "300750.SZ",
                 (NOW - timedelta(hours=1)).isoformat())
    mock = MockLLM([DEBATE_OK])
    res = debate_mod.run_debate(conn, cfg, "300750.SZ", "宁德时代",
                                "+5.0%", "regular", 5.0, now=NOW,
                                llm_call=mock)
    assert res["blocked"] == "debate_dedup_2h" and mock.calls == 0, res

    # ---- 辩论正常 + 低信心降级 + 解析重试 ----
    conn = ledger_mod.init_db(":memory:")
    mock = MockLLM([DEBATE_OK])
    res = debate_mod.run_debate(conn, cfg, "300750.SZ", "宁德时代",
                                "+5.0%", "regular", 5.0, now=NOW,
                                llm_call=mock)
    assert res["verdict"]["confidence"] == 4 and "downgraded" not in \
        res["verdict"], res
    assert ledger_mod.count_judgments(conn, "debate", "2026-10-27") == 1
    conn = ledger_mod.init_db(":memory:")
    mock = MockLLM([DEBATE_LOW])
    res = debate_mod.run_debate(conn, cfg, "300750.SZ", "宁德时代",
                                "+5.0%", "regular", 5.0, now=NOW,
                                llm_call=mock)
    assert res["verdict"]["downgraded"] is True and \
        res["verdict"]["effective_stance"] == "观望", res
    conn = ledger_mod.init_db(":memory:")
    mock = MockLLM(["not json at all", DEBATE_OK])
    res = debate_mod.run_debate(conn, cfg, "300750.SZ", "宁德时代",
                                "+5.0%", "regular", 5.0, now=NOW,
                                llm_call=mock)
    assert mock.calls == 2 and res["verdict"]["confidence"] == 4, res
    conn = ledger_mod.init_db(":memory:")
    mock = MockLLM(["garbage", "garbage"])
    res = debate_mod.run_debate(conn, cfg, "300750.SZ", "宁德时代",
                                "+5.0%", "regular", 5.0, now=NOW,
                                llm_call=mock)
    assert res["error"] == "parse_failed" and mock.calls == 2, res
    assert ledger_mod.count_judgments(conn, "debate", "2026-10-27") == 1

    # ---- 深研正常 + 回测校验 ----
    conn = ledger_mod.init_db(":memory:")
    closes = [100.0 + i * 0.5 for i in range(100)]
    mock = MockLLM([DEEPDIVE_OK])
    res = deepdive_mod.run_deepdive(conn, cfg, "300750.SZ", "宁德时代",
                                    "简报", now=NOW, llm_call=mock,
                                    closes=closes)
    bt = res["report"]["backtest"]
    assert bt["buy_in_range"] is True and bt["touches"] == 21, bt
    assert bt["negative_share_pct"] == 0.0, bt
    assert bt["sell_reached_pct"] == 0.0, bt
    bt2 = backtest_mod.backtest_from_closes(closes, None, None)
    assert "skipped" in bt2
    bt3 = backtest_mod.backtest_from_closes([100.0] * 10, 100.0, None)
    assert "skipped" in bt3
    assert backtest_mod.parse_level("买入 210 元附近") == 210.0
    assert backtest_mod.parse_level(None) is None
    assert backtest_mod.parse_level("无明确依据") is None

    # ---- 哨兵：匹配 / 分级 / 去重 / 推送 ----
    conn = ledger_mod.init_db(":memory:")
    pushes = []

    def recorder(config, title, content, dry_run=False):
        pushes.append((title, content))
        return {"channel": "test", "dry_run": True}

    def summary_mock(system, user, max_tokens):
        return "摘要文本", {}, 0.001

    items = [
        {"title": "宁德时代 300750 公告：停牌核查", "source": "test"},
        {"title": "宁德时代回购股份进展", "source": "test"},
        {"title": "固态电池产业迎政策利好", "source": "test"},
        {"title": "今日调研纪要：某公司日常交流", "source": "test"},
        {"title": "与 watchlist 无关的新闻", "source": "test"},
    ]
    counts = sentinel_mod.process_items(conn, cfg, items, NOW,
                                        push_fn=recorder,
                                        llm_call=summary_mock)
    assert counts == {"matched": 3, "pushed_emergency": 1,
                      "pushed_important": 1, "ledger_only": 1,
                      "skipped_unmatched": 2, "skipped_dup": 0}, counts
    assert len(pushes) == 2, pushes
    assert pushes[0][0].startswith("哨兵紧急"), pushes
    assert "摘要文本" in pushes[1][1], pushes
    rows = conn.execute("SELECT symbol, grade, pushed FROM sentinel_items"
                        " ORDER BY id").fetchall()
    assert rows[0][1] == "emergency" and rows[0][2] == 1, rows
    assert rows[2][0] == "" and rows[2][1] == "routine", rows
    # 重复标题 24h 内 -> 去重
    counts2 = sentinel_mod.process_items(
        conn, cfg, [{"title": "宁德时代 300750 公告：停牌核查",
                    "source": "test"}], NOW, push_fn=recorder)
    assert counts2["skipped_dup"] == 1 and len(pushes) == 2, counts2
    # 联动：debate 诱因简报只含紧急/重要，不含一般
    brief = debate_mod.get_cause_brief(conn, "300750.SZ", NOW)
    assert "停牌核查" in brief and "回购股份" in brief, brief
    assert "调研纪要" not in brief, brief

    # ---- 哨兵：2 天数据源告警（只告警一次）----
    conn = ledger_mod.init_db(":memory:")
    tmp = tempfile.mkdtemp()
    status_path = os.path.join(tmp, "sources.json")
    alerts = []

    def alert_recorder(config, title, content, dry_run=False):
        alerts.append(title)
        return {"channel": "test", "dry_run": True}

    fail_fetch = lambda symbol: ([], "boom")
    cfg2 = make_config()
    cfg2["thresholds"] = {"symbols": {
        "300750.SZ": {"name": "宁德时代", "unit": "pct", "regular": 4.72,
                      "emergency": 7.34}}}
    sentinel_mod.run_sentinel(conn, cfg2, NOW, push_fn=alert_recorder,
                              fetch_fn=fail_fetch, status_path=status_path)
    assert alerts == [], alerts
    sentinel_mod.run_sentinel(conn, cfg2, NOW + timedelta(days=1),
                              push_fn=alert_recorder, fetch_fn=fail_fetch,
                              status_path=status_path)
    assert len(alerts) == 1 and "数据源告警" in alerts[0], alerts
    sentinel_mod.run_sentinel(conn, cfg2, NOW + timedelta(days=2),
                              push_fn=alert_recorder, fetch_fn=fail_fetch,
                              status_path=status_path)
    assert len(alerts) == 1, alerts
    # 未接抓取函数 -> 不记失败、不告警
    res = sentinel_mod.run_sentinel(conn, cfg2, NOW, push_fn=alert_recorder)
    assert res["sources"] == {} and res["counts"]["matched"] == 0, res

    # ---- 集成：触发 -> 辩论（mock）-> 账本 -> 推送 dry-run ----
    conn = ledger_mod.init_db(":memory:")
    mock = MockLLM([DEBATE_OK])
    res = debate_mod.run_debate(conn, cfg, "300750.SZ", "宁德时代",
                                "+5.0%", "regular", 5.0, now=NOW,
                                llm_call=mock)
    assert res.get("blocked") is None
    payload = push_mod.send_alert(cfg, "异动研判：宁德时代",
                                  json.dumps(res["verdict"],
                                             ensure_ascii=False),
                                  dry_run=True)
    assert payload["dry_run"] is True, payload
    assert ledger_mod.count_judgments(conn, "debate", "2026-10-27") == 1

    # ---- 回归：A/B 实测输出的解析与围栏剥离一致性 ----
    ab_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "hidden_files", "ab-test", "results-qwen.json")
    if os.path.exists(ab_path):
        with open(ab_path, encoding="utf-8") as f:
            results = json.load(f)
        checked = 0
        for r in results:
            if "output" not in r:
                continue
            try:
                llm_mod.parse_json(r["output"])
                ok = True
            except ValueError:
                ok = False
            assert ok == bool(r.get("json_ok")), (r["case"], r["task"])
            checked += 1
        assert checked > 0, "no A/B outputs checked"
    print("test_week2: all passed")


if __name__ == "__main__":
    main()
