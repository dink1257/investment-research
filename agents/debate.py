"""辩论（需求 2）：复用 A/B 实测过的 debate 提示词。

为什么是单调用而不是四次调用：A/B 测试验证的就是这个提示词形态
（DeepSeek 155/165、Qwen 147/165，零事实错误）；多方→空方→互驳→裁判
四轮结构在提示词内完整保留。拆成四次调用成本翻几倍且未经验证。
流程（方案 §3.1）：预算检查（含同标的 2 小时去重）-> 哨兵账本取诱因
（近 7 天，无则写明暂无）-> 调 LLM（4000 上限）-> 围栏剥离解析
（失败重试一次，再失败记账并告警，不发半成品）-> 信心降级
（confidence ≤2 自动降为"观望"，不给方向建议，方案 §5）-> 写账本 + 成本。
"""
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agents import llm as llm_mod
from core import budget as budget_mod
from ledger import ledger as ledger_mod

BEIJING = ZoneInfo("Asia/Shanghai")
PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "prompts")
REQUIRED = ["bull", "bear", "bull_rebuttal", "bear_rebuttal", "verdict",
            "target_range", "position", "stop_loss", "confidence",
            "uncertain"]


def load_prompt(name):
    with open(os.path.join(PROMPT_DIR, name), encoding="utf-8") as f:
        return f.read()


def get_cause_brief(conn, symbol, now):
    """哨兵账本近 7 天该标的的紧急/重要项（方案 §3.1 第 3 步：先查账本再搜因）。
    行业项（symbol=''）单列"行业动态"一节，最多 3 条，明确标注，不混入个股项。"""
    since = (now - timedelta(days=7)).isoformat()
    rows = ledger_mod.recent_sentinel(conn, symbol, since, limit=10)
    lines = [f"【{grade}】{title}（{ts[:10]}）"
             for ts, title, grade in rows if grade in ("emergency",
                                                       "important")]
    industry = ledger_mod.recent_sentinel(conn, "", since, limit=3)
    for ts, title, grade in industry:
        if grade in ("emergency", "important"):
            lines.append(f"【行业-{grade}】{title}（{ts[:10]}）")
    return "\n".join(lines)


def _validate(verdict):
    if not isinstance(verdict, dict):
        raise ValueError("结论不是 JSON 对象")
    missing = [k for k in REQUIRED if k not in verdict]
    if missing:
        raise ValueError(f"结论缺字段：{missing}")
    try:
        conf = int(verdict["confidence"])
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"confidence 非整数：{verdict['confidence']}") from e
    verdict["confidence"] = max(1, min(5, conf))
    return verdict


def run_debate(conn, config, symbol, name, move_text, level, move_value,
               is_bp=False, now=None, llm_call=None):
    """开一场辩论。返回 {"verdict": dict, "cost_rmb": float, "blocked": None}
    或 {"blocked": reason}（预算/去重拦截，未调 LLM）或 {"error": ...}。"""
    now = now or datetime.now(BEIJING)
    allowed, reason = budget_mod.check_debate(conn, config, symbol, level,
                                              move_value, is_bp, now)
    if not allowed:
        return {"blocked": reason}
    cause = get_cause_brief(conn, symbol, now) or \
        "暂无（哨兵账本近 7 天无重大项）"
    brief = (f"标的：{name}（{symbol}）；异动：{move_text}；级别：{level}；\n"
             f"诱因要点：\n{cause}")
    prompt = load_prompt("debate.md").replace("{brief}", brief)
    call = llm_call or llm_mod.call_qwen
    total_cost = 0.0
    verdict = None
    last_error = ""
    for _ in range(2):  # 解析失败重试一次
        text, _usage, cost = call(llm_mod.SYSTEM_PROMPT, prompt, 4000)
        total_cost += cost
        try:
            verdict = _validate(llm_mod.parse_json(text))
            break
        except ValueError as e:  # noqa: BLE001
            last_error = str(e)
    budget_mod.record_llm_cost(conn, now, llm_mod.MODEL, total_cost)
    if verdict is None:
        err = {"error": "parse_failed", "detail": last_error}
        ledger_mod.record_judgment(conn, now.isoformat(), "debate", symbol,
                                   f"辩论研判：{name}（{symbol}）",
                                   json.dumps(err, ensure_ascii=False),
                                   total_cost)
        return {"error": "parse_failed", "detail": last_error,
                "cost_rmb": total_cost}
    if verdict["confidence"] <= 2:
        verdict["downgraded"] = True
        verdict["effective_stance"] = "观望"
    ledger_mod.record_judgment(conn, now.isoformat(), "debate", symbol,
                               f"辩论研判：{name}（{symbol}）",
                               json.dumps(verdict, ensure_ascii=False),
                               total_cost)
    return {"verdict": verdict, "cost_rmb": total_cost, "blocked": None}
