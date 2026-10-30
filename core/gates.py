"""门控链：异动触发后、推送前的 7 道门（顺序取自 config gate_chain.order，借鉴 PanWatch）。

方案只定了"同标的 2 小时去重"，其余参数未定死，缺省值如下（均可在配置里覆盖）：
- enabled：标的配置 enabled=false 时拦截；缺省全部启用。
- not_expired：标的配置 expires_at 已过时拦截；缺省不过期（恒过）。
- trading_session：按市场判断是否在交易时段；盘外触发只记账本不推送。
  A 股北京时间 9:30-11:30 / 13:00-15:00；港股 9:30-12:00 / 13:00-16:00；
  美股北京时间 21:30-04:00（夏令时）/ 22:30-05:00（冬令时），跨午夜按起止日折算；
  期货/外汇工作日放行、周末拦截；加密货币全天放行。
  缺口（Week 3 补交易日历）：现阶段只判周末与时段，不判节假日。
- daily_limit：同标的当日已推 >= gate_chain.daily_limit（缺省 3）时拦截。
- cooldown：同标的上次预警距今 < gate_chain.cooldown_minutes（缺省 30）时拦截。
- dedup：同标的同级别在 budget.dedup_hours（缺省 2）小时内已推过时拦截。
- quiet_hours：未配置时恒过；配置为 [start, end)（北京时间小时）时，
  该时段内常规档拦截、紧急档放行。

evaluate 返回 (passed, blocked_by)，blocked_by 为 None 表示全过。
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
EASTERN = ZoneInfo("America/New_York")

DEFAULT_DAILY_LIMIT = 3
DEFAULT_COOLDOWN_MINUTES = 30
DEFAULT_DEDUP_HOURS = 2


def classify_market(symbol):
    """按代码后缀分市场：A / HK / US / futures_fx / crypto。"""
    if symbol.endswith((".SS", ".SZ")):
        return "A"
    if symbol.endswith(".HK"):
        return "HK"
    if symbol.endswith("-USD"):
        return "crypto"
    if (symbol.endswith(("=F", "=X")) or symbol.startswith("^")
            or symbol == "DX-Y.NYB"):
        return "futures_fx"
    return "US"


def _beijing(now):
    if now is None:
        return datetime.now(BEIJING)
    if now.tzinfo is None:
        return now.replace(tzinfo=BEIJING)
    return now.astimezone(BEIJING)


def in_trading_session(symbol, now=None):
    """当前北京时间是否在该标的的交易时段内。"""
    bj = _beijing(now)
    market = classify_market(symbol)
    weekday = bj.weekday()  # 0=周一
    t = bj.time()
    if market == "crypto":
        return True
    if market == "A":
        return weekday < 5 and (
            time(9, 30) <= t < time(11, 30) or time(13, 0) <= t < time(15, 0))
    if market == "HK":
        return weekday < 5 and (
            time(9, 30) <= t < time(12, 0) or time(13, 0) <= t < time(16, 0))
    if market == "futures_fx":
        return weekday < 5
    # 美股：北京时间跨午夜。夏令时 21:30-04:00，冬令时 22:30-05:00。
    dst = _beijing(now).astimezone(EASTERN).dst() != timedelta(0)
    open_t, close_t = (time(21, 30), time(4, 0)) if dst else (
        time(22, 30), time(5, 0))
    evening = weekday < 5 and t >= open_t  # 周一至周五晚开盘
    morning = 0 < weekday < 6 and t < close_t  # 周二至周六凌晨收盘
    return evening or morning


def evaluate(config, conn, symbol, level, symbol_cfg=None, now=None):
    """按配置顺序过 7 道门。返回 (passed, blocked_by)。"""
    bj = _beijing(now)
    gates = config.get("gate_chain", {})
    order = gates.get("order", ["enabled", "not_expired", "trading_session",
                                "daily_limit", "cooldown", "dedup",
                                "quiet_hours"])
    cfg = symbol_cfg or {}
    for gate in order:
        if gate == "enabled":
            if cfg.get("enabled", True) is False:
                return False, "enabled"
        elif gate == "not_expired":
            expires = cfg.get("expires_at")
            if expires:
                exp = datetime.fromisoformat(expires)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=BEIJING)
                if bj >= exp:
                    return False, "not_expired"
        elif gate == "trading_session":
            if not in_trading_session(symbol, bj):
                return False, "trading_session"
        elif gate == "daily_limit":
            limit = gates.get("daily_limit", DEFAULT_DAILY_LIMIT)
            from ledger.ledger import count_alerts_today
            if count_alerts_today(conn, symbol,
                                  bj.strftime("%Y-%m-%d")) >= limit:
                return False, "daily_limit"
        elif gate == "cooldown":
            minutes = gates.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
            from ledger.ledger import count_alerts_since
            since = (bj - timedelta(minutes=minutes)).isoformat()
            if count_alerts_since(conn, symbol, since) > 0:
                return False, "cooldown"
        elif gate == "dedup":
            hours = config.get("budget", {}).get("dedup_hours",
                                                DEFAULT_DEDUP_HOURS)
            from ledger.ledger import count_alerts_since
            since = (bj - timedelta(hours=hours)).isoformat()
            if count_alerts_since(conn, symbol, since, level=level) > 0:
                return False, "dedup"
        elif gate == "quiet_hours":
            qh = gates.get("quiet_hours")
            if qh and level != "emergency":
                start, end = qh[0], qh[1]
                hour = bj.hour
                in_quiet = (start <= hour < end) if start <= end else (
                    hour >= start or hour < end)
                if in_quiet:
                    return False, "quiet_hours"
    return True, None
