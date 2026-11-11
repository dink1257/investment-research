"""last_analysis 结果文件（Hermes 契约 v2）：写出、三种 payload、
深研 CLI 接线（成功/阻塞/失败都写 state/ 文件）、deadline 检查。无网络。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

from tools import last_analysis as la_mod  # noqa: E402
import run_debate as run_debate_mod  # noqa: E402
import run_deepdive as run_deepdive_mod  # noqa: E402
from agents import deepdive as deepdive_mod  # noqa: E402

TS = "2026-11-10T22:30:00+08:00"


def test_write_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "state")
        payload = {"schema_version": 2, "type": "deepdive",
                   "symbol": "300750.SZ"}
        path = la_mod.write_last_analysis(state, payload)
        assert path == os.path.join(state, "last_analysis.json"), path
        with open(path, encoding="utf-8") as f:
            assert json.load(f) == payload


def test_deepdive_payload():
    report = {"verdict": "观望：数据不足", "buy_point": "60 元附近",
              "sell_point": None, "risk_strategy": "等数据",
              "uncertain": ["收入增速能否延续"],
              "backtest": {"skipped": "x"}}
    p = la_mod.deepdive_payload("300750.SZ", "宁德时代", report, 0.0062, TS,
                               task_id="T-1", budget_cny=0.5,
                               deadline_utc="2026-11-11T10:00:00Z")
    assert p["schema_version"] == 2
    assert p["type"] == "deepdive"
    assert p["mode"] == "deepdive"
    assert p["status"] == "success"
    assert p["task_id"] == "T-1"
    assert p["symbol"] == "300750.SZ"
    assert p["name"] == "宁德时代"
    assert p["verdict"] == "观望：数据不足"
    assert p["confidence"] is None  # 深研不产置信度，不编
    assert p["report_path"] == "state/last_analysis.md"
    assert p["report"] is report
    assert p["cost_cny"] == 0.0062
    assert p["cost_rmb"] == 0.0062  # 旧字段同值别名
    assert p["budget_cny"] == 0.5
    assert p["deadline_utc"] == "2026-11-11T10:00:00Z"
    assert p["finished_at"] == TS
    assert p["key_points"][0] == "观望：数据不足"
    assert any("60 元附近" in k for k in p["key_points"])
    assert p["falsify"] == ["收入增速能否延续"]


def test_debate_payload():
    verdict = {"verdict": "看多：订单饱满", "confidence": 3,
               "target_range": "70-80", "position": "轻仓",
               "stop_loss": "跌破 60", "bull": "b", "bear": "c",
               "bull_rebuttal": "br", "bear_rebuttal": "cr",
               "uncertain": ["u1"]}
    p = la_mod.debate_payload("人形机器人板块", verdict, 0.0091, TS,
                            task_id="T-2")
    assert p["type"] == "debate"
    assert p["mode"] == "debate"
    assert p["status"] == "success"
    assert p["task_id"] == "T-2"
    assert p["symbol"] == "topic:人形机器人板块"
    assert p["topic"] == "人形机器人板块"
    assert p["verdict"] == "看多：订单饱满"
    assert p["confidence"] == 3
    assert p["report"] is verdict
    assert p["report_path"] == "state/last_analysis.md"
    assert p["cost_cny"] == 0.0091
    assert p["key_points"][0] == "看多：订单饱满"
    assert "u1" in p["falsify"] and "跌破 60" in p["falsify"]


def test_running_blocked_failed_payloads():
    r = la_mod.running_payload("T-3", "deepdive", symbol="300750.SZ",
                               ts=TS)
    assert r["status"] == "running" and r["task_id"] == "T-3"
    assert r["symbol"] == "300750.SZ" and r["verdict"] is None
    b = la_mod.blocked_payload("T-3", "debate", "debate_dedup_2h",
                               topic="人形机器人板块", ts=TS)
    assert b["status"] == "blocked" and b["blocked"] == "debate_dedup_2h"
    assert b["cost_cny"] == 0.0
    f = la_mod.failed_payload("T-3", "deepdive", "parse_failed", "bad json",
                              0.02, symbol="300750.SZ", ts=TS)
    assert f["status"] == "failed" and f["error"] == "parse_failed"
    assert f["detail"] == "bad json" and f["cost_cny"] == 0.02


def test_parse_deadline_and_budget():
    assert run_debate_mod.parse_deadline("2026-11-11T10:00:00Z") is not None
    assert run_debate_mod.parse_deadline("") is None
    assert run_debate_mod.parse_deadline("not-a-date") is None
    assert run_debate_mod.parse_budget("") is None
    assert run_debate_mod.parse_budget(None) is None
    assert run_debate_mod.parse_budget("0.5") == 0.5


def _write_config(path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("budget:\n  monthly_cap: 20\n  warn_at: 16\n"
                "  daily_debate_cap: 8\n  daily_deepdive_cap: 2\n"
                "  dedup_hours: 2\nthresholds:\n  symbols: {}\n")


def _run_cli(mod, argv):
    old_argv = sys.argv
    try:
        sys.argv = argv
        return mod.main()
    finally:
        sys.argv = old_argv


def test_deepdive_cli_writes_result_files():
    """深研 CLI 成功后写 state/last_analysis.json + .md（带 task_id）。"""
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        report = {"verdict": "观望：数据不足", "buy_point": None,
                  "sell_point": None, "risk_strategy": "等数据",
                  "fundamental": "f", "technical": "t", "capital": "c",
                  "risks": "r", "uncertain": ["u"],
                  "backtest": {"skipped": "buy_point 无数值"}}
        old = deepdive_mod.run_deepdive
        deepdive_mod.run_deepdive = lambda *a, **k: {
            "report": report, "cost_rmb": 0.0062, "blocked": None}
        try:
            assert _run_cli(run_deepdive_mod,
                            ["run_deepdive.py", "--config", cfg, "--db", db,
                             "--symbol", "300750.SZ", "--brief", "测试",
                             "--task-id", "T-9", "--budget-cny", "0.5",
                             "--deadline-utc", "2099-01-01T00:00:00Z",
                             "--state-dir", state]) == 0
        finally:
            deepdive_mod.run_deepdive = old
        with open(os.path.join(state, "last_analysis.json"),
                  encoding="utf-8") as f:
            payload = json.load(f)
        assert payload["type"] == "deepdive"
        assert payload["status"] == "success"
        assert payload["task_id"] == "T-9"
        assert payload["symbol"] == "300750.SZ"
        assert payload["verdict"] == "观望：数据不足"
        assert payload["cost_cny"] == 0.0062
        assert payload["budget_cny"] == 0.5
        assert payload["falsify"] == ["u"]
        assert not os.path.exists(os.path.join(d, "data",
                                              "last_analysis.json"))
        with open(os.path.join(state, "last_analysis.md"),
                  encoding="utf-8") as f:
            assert "观望：数据不足" in f.read()


def test_deepdive_cli_blocked_writes_blocked_file():
    """阻塞也写文件（status blocked），不再是“什么都不写”。"""
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        old = deepdive_mod.run_deepdive
        deepdive_mod.run_deepdive = lambda *a, **k: {
            "blocked": "daily_deepdive_cap"}
        try:
            assert _run_cli(run_deepdive_mod,
                            ["run_deepdive.py", "--config", cfg, "--db", db,
                             "--symbol", "300750.SZ", "--task-id", "T-10",
                             "--state-dir", state]) == 2
        finally:
            deepdive_mod.run_deepdive = old
        with open(os.path.join(state, "last_analysis.json"),
                  encoding="utf-8") as f:
            payload = json.load(f)
        assert payload["status"] == "blocked"
        assert payload["blocked"] == "daily_deepdive_cap"
        assert payload["task_id"] == "T-10"
        with open(os.path.join(state, "last_analysis.md"),
                  encoding="utf-8") as f:
            assert "daily_deepdive_cap" in f.read()


def test_deepdive_cli_failed_writes_failed_file():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        old = deepdive_mod.run_deepdive
        deepdive_mod.run_deepdive = lambda *a, **k: {
            "error": "parse_failed", "detail": "bad json",
            "cost_rmb": 0.02}
        try:
            assert _run_cli(run_deepdive_mod,
                            ["run_deepdive.py", "--config", cfg, "--db", db,
                             "--symbol", "300750.SZ", "--task-id", "T-11",
                             "--state-dir", state]) == 1
        finally:
            deepdive_mod.run_deepdive = old
        with open(os.path.join(state, "last_analysis.json"),
                  encoding="utf-8") as f:
            payload = json.load(f)
        assert payload["status"] == "failed"
        assert payload["error"] == "parse_failed"
        assert payload["cost_cny"] == 0.02


def test_deepdive_cli_deadline_exceeded_writes_blocked():
    """过了 deadline 不调 LLM，直接写 blocked。"""
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        called = []
        old = deepdive_mod.run_deepdive
        deepdive_mod.run_deepdive = lambda *a, **k: called.append(True) or {
            "report": {}, "cost_rmb": 0.0, "blocked": None}
        try:
            assert _run_cli(run_deepdive_mod,
                            ["run_deepdive.py", "--config", cfg, "--db", db,
                             "--symbol", "300750.SZ", "--task-id", "T-12",
                             "--deadline-utc", "2020-01-01T00:00:00Z",
                             "--state-dir", state]) == 2
        finally:
            deepdive_mod.run_deepdive = old
        assert called == []  # 未调 LLM
        with open(os.path.join(state, "last_analysis.json"),
                  encoding="utf-8") as f:
            payload = json.load(f)
        assert payload["status"] == "blocked"
        assert payload["blocked"] == "deadline_exceeded"
        assert payload["cost_cny"] == 0.0


def main():
    test_write_roundtrip()
    test_deepdive_payload()
    test_debate_payload()
    test_running_blocked_failed_payloads()
    test_parse_deadline_and_budget()
    test_deepdive_cli_writes_result_files()
    test_deepdive_cli_blocked_writes_blocked_file()
    test_deepdive_cli_failed_writes_failed_file()
    test_deepdive_cli_deadline_exceeded_writes_blocked()
    print("test_last_analysis: all passed")


if __name__ == "__main__":
    main()
