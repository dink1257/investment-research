"""预算与熔断（方案 §7.3，写进代码）。

- 日配额：辩论 ≤8/天、深研 ≤2/天（账本 judgments 计数）。
- 月熔断：累计 ≥¥16 → 只放紧急异动（pct 型要求 |涨跌幅| ≥5%；
  bp 型只要求紧急档，因基点紧急阈值本身已是极端 move）；
  ≥¥20 → 停一切非必要调用（辩论、深研、摘要），只剩纯计算监控 + 推送。
- 成本记账：每次 LLM 调用后记 costs 表（日期、模型、次数、金额）。
- 金额单位：人民币元；月累计按北京时间 YYYY-MM 前缀汇总。
"""
from datetime import timedelta

from ledger import ledger as ledger_mod

MONTHLY_WARN = 16.0
MONTHLY_CAP = 20.0
DAILY_DEBATE_CAP = 8
DAILY_DEEPDIVE_CAP = 2
EMERGENCY_MIN_PCT = 5.0


def _date_str(now):
    return now.strftime("%Y-%m-%d")


def _month_str(now):
    return now.strftime("%Y-%m")


def record_llm_cost(conn, now, model, cost_rmb):
    ledger_mod.add_cost(conn, _date_str(now), model, 1, cost_rmb)


def check_debate(conn, config, symbol, level, move, is_bp, now):
    """辩论放行检查。返回 (allowed, reason)，reason 为 None 表示放行。"""
    budget = config.get("budget", {})
    monthly = ledger_mod.sum_costs_month(conn, _month_str(now))
    if monthly >= budget.get("monthly_cap", MONTHLY_CAP):
        return False, "monthly_cap_20"
    if monthly >= budget.get("warn_at", MONTHLY_WARN):
        big_enough = is_bp or abs(move or 0) >= EMERGENCY_MIN_PCT
        if not (level == "emergency" and big_enough):
            return False, "monthly_warn_16"
    if ledger_mod.count_judgments(conn, "debate", _date_str(now)) >= \
            budget.get("daily_debate_cap", DAILY_DEBATE_CAP):
        return False, "daily_debate_cap"
    dedup_hours = budget.get("dedup_hours", 2)
    since = (now - timedelta(hours=dedup_hours)).isoformat()
    if ledger_mod.count_judgments_since(conn, "debate", symbol, since) > 0:
        return False, "debate_dedup_2h"
    return True, None


def check_deepdive(conn, config, now):
    """按需深研放行检查。月累计 ≥¥16 即暂停（只保留紧急异动辩论）。"""
    budget = config.get("budget", {})
    monthly = ledger_mod.sum_costs_month(conn, _month_str(now))
    if monthly >= budget.get("monthly_cap", MONTHLY_CAP):
        return False, "monthly_cap_20"
    if monthly >= budget.get("warn_at", MONTHLY_WARN):
        return False, "monthly_warn_16"
    if ledger_mod.count_judgments(conn, "deepdive", _date_str(now)) >= \
            budget.get("daily_deepdive_cap", DAILY_DEEPDIVE_CAP):
        return False, "daily_deepdive_cap"
    return True, None


def check_summary(conn, config, now):
    """哨兵摘要（非必要调用）：月累计 ≥¥20 才停，其余放行。"""
    budget = config.get("budget", {})
    monthly = ledger_mod.sum_costs_month(conn, _month_str(now))
    if monthly >= budget.get("monthly_cap", MONTHLY_CAP):
        return False, "monthly_cap_20"
    return True, None
