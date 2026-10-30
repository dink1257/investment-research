"""按需深研（需求 3，你点名触发）：复用 A/B 实测过的 deepdive 提示词。

单调用内含四分析师（基本面/技术面/资金面/风险只挑毛病）+ 综合；
买卖点随后由 core/backtest.py 做确定性历史回放校验，不由 LLM 拍脑袋。
流程：预算检查（≤2/天；月累计 ≥¥16 暂停按需深研）-> 调 LLM（8000 上限）
-> 围栏剥离解析（失败重试一次，再失败记账，不发半成品）-> 回测校验
-> 写账本 + 成本。深研结论同样带 uncertain 清单与证伪条件（提示词内要求）。
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from agents import llm as llm_mod
from core import backtest as backtest_mod
from core import budget as budget_mod
from ledger import ledger as ledger_mod

BEIJING = ZoneInfo("Asia/Shanghai")
PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "prompts")
REQUIRED = ["fundamental", "technical", "capital", "risks", "buy_point",
            "sell_point", "risk_strategy", "verdict", "uncertain"]


def load_prompt(name):
    with open(os.path.join(PROMPT_DIR, name), encoding="utf-8") as f:
        return f.read()


def _validate(report):
    if not isinstance(report, dict):
        raise ValueError("结论不是 JSON 对象")
    missing = [k for k in REQUIRED if k not in report]
    if missing:
        raise ValueError(f"结论缺字段：{missing}")
    return report


def run_deepdive(conn, config, symbol, name, brief, now=None, llm_call=None,
                 closes=None):
    """做一次深研。返回 {"report": dict, "cost_rmb": float, "blocked": None}
    或 {"blocked": reason} 或 {"error": ...}。"""
    now = now or datetime.now(BEIJING)
    allowed, reason = budget_mod.check_deepdive(conn, config, now)
    if not allowed:
        return {"blocked": reason}
    prompt = load_prompt("deepdive.md").replace(
        "{brief}", f"标的：{name}（{symbol}）。\n{brief}")
    call = llm_call or llm_mod.call_qwen
    total_cost = 0.0
    report = None
    last_error = ""
    for _ in range(2):  # 解析失败重试一次
        text, _usage, cost = call(llm_mod.SYSTEM_PROMPT, prompt, 8000)
        total_cost += cost
        try:
            report = _validate(llm_mod.parse_json(text))
            break
        except ValueError as e:  # noqa: BLE001
            last_error = str(e)
    budget_mod.record_llm_cost(conn, now, llm_mod.MODEL, total_cost)
    if report is None:
        err = {"error": "parse_failed", "detail": last_error}
        ledger_mod.record_judgment(conn, now.isoformat(), "deepdive", symbol,
                                   f"深研报告：{name}（{symbol}）",
                                   json.dumps(err, ensure_ascii=False),
                                   total_cost)
        return {"error": "parse_failed", "detail": last_error,
                "cost_rmb": total_cost}
    buy = backtest_mod.parse_level(report.get("buy_point"))
    sell = backtest_mod.parse_level(report.get("sell_point"))
    try:
        data = closes if closes is not None else \
            backtest_mod.fetch_closes(symbol)
        report["backtest"] = backtest_mod.backtest_from_closes(data, buy,
                                                               sell)
    except ValueError as e:  # noqa: BLE001
        report["backtest"] = {"skipped": str(e)}
    ledger_mod.record_judgment(conn, now.isoformat(), "deepdive", symbol,
                               f"深研报告：{name}（{symbol}）",
                               json.dumps(report, ensure_ascii=False),
                               total_cost)
    return {"report": report, "cost_rmb": total_cost, "blocked": None}
