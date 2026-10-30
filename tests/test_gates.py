"""门控链单元测试（无网络、无第三方依赖，直接跑）。"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import gates  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402

BJ = gates.BEIJING


def dt(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=BJ)


def main():
    # ---- 交易时段 ----
    assert gates.in_trading_session("600519.SS", dt(2026, 10, 27, 10, 0))
    assert not gates.in_trading_session("600519.SS", dt(2026, 10, 27, 12, 0))
    assert not gates.in_trading_session("600519.SS", dt(2026, 11, 1, 10, 0))
    assert gates.in_trading_session("0700.HK", dt(2026, 10, 27, 10, 0))
    assert not gates.in_trading_session("0700.HK", dt(2026, 10, 27, 12, 30))
    # 美股：10-27 夏令时，北京时间 22:00 在盘中、10:00 不在
    assert gates.in_trading_session("META", dt(2026, 10, 27, 22, 0))
    assert not gates.in_trading_session("META", dt(2026, 10, 27, 10, 0))
    # 美股：11-03 冬令时，23:00 在盘中、22:00 未开盘
    assert gates.in_trading_session("META", dt(2026, 11, 3, 23, 0))
    assert not gates.in_trading_session("META", dt(2026, 11, 3, 22, 0))
    assert gates.in_trading_session("BTC-USD", dt(2026, 11, 1, 10, 0))
    assert gates.in_trading_session("BZ=F", dt(2026, 10, 27, 10, 0))
    assert not gates.in_trading_session("BZ=F", dt(2026, 11, 1, 10, 0))

    cfg = {"budget": {"dedup_hours": 2}, "gate_chain": {}}

    def fresh():
        return ledger_mod.init_db(":memory:")

    now = datetime.now(BJ)
    # ---- 全过（加密货币，无时段限制）----
    assert gates.evaluate(cfg, fresh(), "BTC-USD", "regular", {}, now) \
        == (True, None)
    # ---- enabled / not_expired ----
    assert gates.evaluate(cfg, fresh(), "BTC-USD", "regular",
                          {"enabled": False}, now) == (False, "enabled")
    assert gates.evaluate(cfg, fresh(), "BTC-USD", "regular",
                          {"expires_at": "2020-01-01T00:00:00+08:00"},
                          now) == (False, "not_expired")
    assert gates.evaluate(cfg, fresh(), "BTC-USD", "regular",
                          {"expires_at": "2999-01-01T00:00:00+08:00"},
                          now) == (True, None)
    # ---- 每日上限（缺省 3）----
    conn = fresh()
    for _ in range(3):
        ledger_mod.record_alert(conn, now.isoformat(), "BTC-USD", "regular",
                                5.0, "t", pushed=1)
    assert gates.evaluate(cfg, conn, "BTC-USD", "regular", {}, now) \
        == (False, "daily_limit")
    # ---- 冷却（缺省 30 分钟）----
    conn = fresh()
    ledger_mod.record_alert(conn, (now - timedelta(minutes=10)).isoformat(),
                            "BTC-USD", "regular", 5.0, "t", pushed=1)
    assert gates.evaluate(cfg, conn, "BTC-USD", "regular", {}, now) \
        == (False, "cooldown")
    # ---- 去重（同级别 2 小时内）----
    conn = fresh()
    ledger_mod.record_alert(conn, (now - timedelta(hours=1)).isoformat(),
                            "BTC-USD", "regular", 5.0, "t", pushed=1)
    assert gates.evaluate(cfg, conn, "BTC-USD", "regular", {}, now) \
        == (False, "dedup")
    assert gates.evaluate(cfg, conn, "BTC-USD", "emergency", {}, now) \
        == (True, None)
    # ---- 安静时段（未配置恒过；配置后常规档拦、紧急档过）----
    assert gates.evaluate(cfg, fresh(), "BTC-USD", "regular", {},
                          dt(2026, 10, 27, 3, 0)) == (True, None)
    cfg_q = {"budget": {"dedup_hours": 2},
             "gate_chain": {"quiet_hours": [0, 7]}}
    assert gates.evaluate(cfg_q, fresh(), "BTC-USD", "regular", {},
                          dt(2026, 10, 27, 3, 0)) == (False, "quiet_hours")
    assert gates.evaluate(cfg_q, fresh(), "BTC-USD", "emergency", {},
                          dt(2026, 10, 27, 3, 0)) == (True, None)
    print("test_gates: all passed")


if __name__ == "__main__":
    main()
