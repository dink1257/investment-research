"""冒烟测试：账本读写 + 推送 dry-run + Yahoo 实测 + 阈值联动。

Yahoo 实测需联网；深夜/休市时 pct 接近 0 属正常（只校验取数成功与数值合理）。
腾讯交叉校验仅作诊断打印（备源映射若有偏差，只警告不判失败，影子期再校准）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from core.quotes import (DisagreementError, cross_validate, fetch,
                         fetch_tencent, fetch_yahoo)
from core.thresholds import classify
from ledger import ledger
from push import push


def main():
    # 1. 账本读写（内存库）
    conn = ledger.init_db(":memory:")
    ledger.record_snapshot(conn, "2026-10-26T02:50:00+08:00", "600519.SS",
                           "贵州茅台", 1700.0, 1690.0, 0.59, "none", "yahoo")
    ledger.record_judgment(conn, "2026-10-26T02:50:00+08:00", "debate",
                           "600519.SS", "冒烟测试", '{"verdict": "观望"}', 0.01)
    ledger.record_sentinel(conn, "2026-10-26T02:50:00+08:00", "600519.SS",
                           "测试源", "冒烟哨兵项", "routine")
    ledger.add_cost(conn, "2026-10-26", "qwen3.8-flash", 1, 0.01)
    ledger.update_heartbeat(conn, "2026-10-26T02:50:00+08:00", "ok", "冒烟")
    assert ledger.get_heartbeat(conn)[1] == "ok"
    assert len(ledger.get_recent_snapshots(conn)) == 1
    print("账本读写 OK")

    # 2. 推送 dry-run（无真实请求）
    config = yaml.safe_load(open(
        os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "config.yaml"), encoding="utf-8"))
    res = push.send_alert(config, "冒烟标题", "冒烟内容", dry_run=True)
    assert res["dry_run"] is True and res["channel"] == "feishu"
    print("推送 dry-run OK（主通道，不重复投递）")

    # 3. Yahoo 实测 + 阈值联动
    for symbol in ("600519.SS", "AAPL"):
        q = fetch(symbol)
        assert q["price"] > 0 and q["prev_close"] > 0 and q["pct"] is not None
        assert abs(q["pct"]) < 30, f"{symbol} 涨跌幅异常：{q['pct']}"
        cfg = config["thresholds"]["symbols"].get(symbol)
        level = classify(cfg, pct=q["pct"]) if cfg else "unknown"
        print(f"Yahoo 实测 {symbol}：现价 {q['price']} 前收 {q['prev_close']} "
              f"涨跌 {q['pct']:.2f}% -> {level}")

    # 4. 腾讯交叉校验（诊断）
    try:
        yahoo, tencent = cross_validate("600519.SS")
        if tencent is None:
            print("腾讯备源不可用（主源正常，不阻塞）")
        else:
            print(f"双源一致：Yahoo {yahoo['pct']:.2f}% vs "
                  f"腾讯 {tencent['pct']:.2f}%")
    except DisagreementError as e:
        print(f"警告（双源不一致，需影子期校准）：{e}")
    except Exception as e:  # noqa: BLE001
        print(f"腾讯诊断跳过：{e}")

    # 5. ^TNX 基点换算
    q = fetch_yahoo("^TNX")
    from core.quotes import bp_change
    print(f"^TNX 实测：收益率 {q['price']} 前值 {q['prev_close']} "
          f"变化 {bp_change(q['prev_close'], q['price']):.1f}bp")
    print("冒烟测试全部通过")


if __name__ == "__main__":
    main()
