"""阈值引擎：纯计算，零 LLM 依赖。

规则（方案 §3.5）：每标的常规阈值 = 过去 252 交易日 |日收益| 的 95 分位数，
紧急阈值 = 99 分位数，每月滚动刷新；^TNX 用基点阈值（常规 ±10bp、紧急 ±20bp）。
边界含等于：达到阈值即触发（>=）。
"""


def pct_change(prev_close, close):
    """由前收与现价算涨跌幅（百分比）。算不出返回 None（调用方跳过，不猜数）。"""
    if prev_close is None or close is None or prev_close == 0:
        return None
    return (close - prev_close) / prev_close * 100.0


def classify(cfg, pct=None, bp=None):
    """判定异动级别，返回 'emergency' | 'regular' | 'none'。

    cfg 为百分比型 {unit: 'pct', regular, emergency} 或基点型
    {unit: 'bp', regular_bp, emergency_bp}；比较用绝对值（涨跌同等对待）。
    """
    if cfg.get("unit") == "bp":
        if bp is None:
            return "none"
        v = abs(bp)
        if v >= cfg["emergency_bp"]:
            return "emergency"
        if v >= cfg["regular_bp"]:
            return "regular"
        return "none"
    if pct is None:
        return "none"
    v = abs(pct)
    if v >= cfg["emergency"]:
        return "emergency"
    if v >= cfg["regular"]:
        return "regular"
    return "none"
