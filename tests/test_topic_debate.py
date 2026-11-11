"""主题辩论：run_topic_debate 与 run_debate CLI。LLM 全部 mock，无网络。"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

from agents import debate as debate_mod  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402
import run_debate as run_debate_mod  # noqa: E402

BJ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 10, 22, 30, tzinfo=BJ)

VERDICT = {
    "bull": "多方：订单饱满，收入翻倍",
    "bear": "空方：仍未盈利，市销率超 14 倍",
    "bull_rebuttal": "多方反驳：收入增速能消化估值",
    "bear_rebuttal": "空方反驳：增速回落则估值承压",
    "verdict": "看多：短期催化明确，但估值已高",
    "target_range": "70-80",
    "position": "轻仓",
    "stop_loss": "跌破 60 或催化证伪",
    "confidence": 3,
    "uncertain": ["下半年收入增速能否延续"],
}


def make_config():
    return {"budget": {"monthly_cap": 20, "warn_at": 16,
                       "daily_debate_cap": 8, "daily_deepdive_cap": 2,
                       "dedup_hours": 2}}


class MockLLM:
    """按顺序返回 canned 输出；记录最后一次收到的提示词。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0
        self.last_user = ""

    def __call__(self, system, user, max_tokens):
        self.calls += 1
        self.last_user = user
        text = self.outputs.pop(0) if self.outputs else "{}"
        return text, {}, 0.01


def _verdict_json(confidence=None):
    v = dict(VERDICT)
    if confidence is not None:
        v["confidence"] = confidence
    return json.dumps(v, ensure_ascii=False)


def test_topic_debate_success():
    conn = ledger_mod.init_db(":memory:")
    mock = MockLLM([_verdict_json()])
    res = debate_mod.run_topic_debate(conn, make_config(), "人形机器人板块",
                                      now=NOW, llm_call=mock)
    assert res.get("blocked") is None, res
    assert res["verdict"]["confidence"] == 3
    assert res["cost_rmb"] == 0.01
    assert res["topic"] == "人形机器人板块"
    assert ledger_mod.count_judgments(conn, "debate", "2026-11-10") == 1
    assert ledger_mod.count_judgments_since(
        conn, "debate", "topic:人形机器人板块",
        (NOW - timedelta(hours=2)).isoformat()) == 1
    assert "人形机器人板块" in mock.last_user
    assert "暂无" in mock.last_user  # 无行业项时写明暂无


def test_topic_debate_industry_brief():
    conn = ledger_mod.init_db(":memory:")
    ledger_mod.record_sentinel(conn, NOW.isoformat(), "", "akshare",
                               "人形机器人新品发布", "important")
    mock = MockLLM([_verdict_json()])
    res = debate_mod.run_topic_debate(conn, make_config(), "人形机器人板块",
                                      now=NOW, llm_call=mock)
    assert res.get("verdict") is not None, res
    assert "人形机器人新品发布" in mock.last_user
    assert "行业-important" in mock.last_user


def test_topic_debate_downgrade():
    conn = ledger_mod.init_db(":memory:")
    mock = MockLLM([_verdict_json(confidence=2)])
    res = debate_mod.run_topic_debate(conn, make_config(), "人形机器人板块",
                                      now=NOW, llm_call=mock)
    assert res["verdict"]["downgraded"] is True
    assert res["verdict"]["effective_stance"] == "观望"


def test_topic_debate_dedup_per_topic():
    conn = ledger_mod.init_db(":memory:")
    mock = MockLLM([_verdict_json(), _verdict_json()])
    cfg = make_config()
    r1 = debate_mod.run_topic_debate(conn, cfg, "人形机器人板块",
                                     now=NOW, llm_call=mock)
    assert r1.get("verdict") is not None, r1
    r2 = debate_mod.run_topic_debate(conn, cfg, "人形机器人板块",
                                     now=NOW + timedelta(minutes=30),
                                     llm_call=mock)
    assert r2.get("blocked") == "debate_dedup_2h", r2
    r3 = debate_mod.run_topic_debate(conn, cfg, "固态电池量产进度",
                                     now=NOW + timedelta(minutes=30),
                                     llm_call=mock)
    assert r3.get("verdict") is not None, r3  # 不同主题互不拦截


def test_topic_debate_parse_failed():
    conn = ledger_mod.init_db(":memory:")
    mock = MockLLM(["not json", "still not json"])
    res = debate_mod.run_topic_debate(conn, make_config(), "人形机器人板块",
                                      now=NOW, llm_call=mock)
    assert res.get("error") == "parse_failed", res
    assert res["cost_rmb"] == 0.02
    assert mock.calls == 2  # 解析失败重试一次
    assert ledger_mod.count_judgments(conn, "debate", "2026-11-10") == 1


def test_topic_debate_monthly_cap():
    conn = ledger_mod.init_db(":memory:")
    ledger_mod.add_cost(conn, "2026-11-10", "qwen3.8-flash", 1, 20.0)
    mock = MockLLM([_verdict_json()])
    res = debate_mod.run_topic_debate(conn, make_config(), "人形机器人板块",
                                      now=NOW, llm_call=mock)
    assert res.get("blocked") == "monthly_cap_20", res
    assert mock.calls == 0


def test_run_debate_regression():
    """重构回归：个股辩论行为不变（提示词、校验、记账、返回结构）。"""
    conn = ledger_mod.init_db(":memory:")
    mock = MockLLM([_verdict_json()])
    res = debate_mod.run_debate(conn, make_config(), "9880.HK", "优必选",
                                "+10.16%", "emergency", 10.16,
                                now=NOW, llm_call=mock)
    assert res.get("blocked") is None, res
    assert res["verdict"]["confidence"] == 3
    assert res["cost_rmb"] == 0.01
    assert ledger_mod.count_judgments(conn, "debate", "2026-11-10") == 1
    assert "优必选" in mock.last_user
    assert "9880.HK" in mock.last_user


def _write_config(path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("budget:\n  monthly_cap: 20\n  warn_at: 16\n"
                "  daily_debate_cap: 8\n  daily_deepdive_cap: 2\n"
                "  dedup_hours: 2\nthresholds:\n  symbols: {}\n")


def test_run_debate_cli_writes_result_files():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        verdict = dict(VERDICT)
        old = debate_mod.run_topic_debate
        debate_mod.run_topic_debate = lambda *a, **k: {
            "verdict": verdict, "cost_rmb": 0.0091, "blocked": None,
            "topic": "人形机器人板块"}
        old_argv = sys.argv
        try:
            sys.argv = ["run_debate.py", "--config", cfg, "--db", db,
                        "--topic", "人形机器人板块", "--task-id", "T-20",
                        "--state-dir", state]
            assert run_debate_mod.main() == 0
        finally:
            debate_mod.run_topic_debate = old
            sys.argv = old_argv
        with open(os.path.join(state, "last_analysis.json"),
                  encoding="utf-8") as f:
            payload = json.load(f)
        assert payload["type"] == "debate"
        assert payload["status"] == "success"
        assert payload["task_id"] == "T-20"
        assert payload["topic"] == "人形机器人板块"
        assert payload["symbol"] == "topic:人形机器人板块"
        assert payload["confidence"] == 3
        assert payload["cost_cny"] == 0.0091
        assert payload["key_points"][0].startswith("看多")
        assert "下半年收入增速能否延续" in payload["falsify"]
        assert not os.path.exists(os.path.join(d, "data",
                                              "last_analysis.json"))
        with open(os.path.join(state, "last_analysis.md"),
                  encoding="utf-8") as f:
            assert "短期催化明确" in f.read()


def test_run_debate_cli_blocked_writes_blocked_file():
    """阻塞也写文件（status blocked），不再是“什么都不写”。"""
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        old = debate_mod.run_topic_debate
        debate_mod.run_topic_debate = lambda *a, **k: {
            "blocked": "debate_dedup_2h"}
        old_argv = sys.argv
        try:
            sys.argv = ["run_debate.py", "--config", cfg, "--db", db,
                        "--topic", "人形机器人板块", "--task-id", "T-21",
                        "--state-dir", state]
            assert run_debate_mod.main() == 2
        finally:
            debate_mod.run_topic_debate = old
            sys.argv = old_argv
        with open(os.path.join(state, "last_analysis.json"),
                  encoding="utf-8") as f:
            payload = json.load(f)
        assert payload["status"] == "blocked"
        assert payload["blocked"] == "debate_dedup_2h"
        assert payload["task_id"] == "T-21"


def main():
    test_topic_debate_success()
    test_topic_debate_industry_brief()
    test_topic_debate_downgrade()
    test_topic_debate_dedup_per_topic()
    test_topic_debate_parse_failed()
    test_topic_debate_monthly_cap()
    test_run_debate_regression()
    test_run_debate_cli_writes_result_files()
    test_run_debate_cli_blocked_writes_blocked_file()
    print("test_topic_debate: all passed")


if __name__ == "__main__":
    main()
