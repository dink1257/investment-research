"""监控轮询：单次循环（供定时任务每 15 分钟调用一次）。

流程：读配置 -> 并行拉行情（A 股双源交叉校验）-> 阈值判定 -> 写快照
-> 触发的过门控链 -> 放行的推送（凭证为空或 dry-run 时只返回 payload）
-> 心跳落盘（heartbeat.json + 账本 heartbeat 行）。

绝不静默：行情全失败、双源不一致都计入心跳 detail，不吞掉。
本次不部署：本文件只跑单次循环，定时调度（Actions cron + watchdog）是部署步骤。
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402
from core import gates, quotes, thresholds  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402
from push import push as push_mod  # noqa: E402

BEIJING = ZoneInfo("Asia/Shanghai")
TOLERANCE_PP = 0.5


def fetch_symbol(symbol):
    """拉取一标的。返回 (quote, disagreement, backup_missing)。

    A 股走双源：Yahoo 为主、腾讯为备，涨跌幅差 > 0.5pp 即不一致；
    非 A 股走主备链（Yahoo 优先，失败则腾讯——腾讯仅支持 A 股，
    故非 A 股实际只有 Yahoo 一源，失败即 QuoteError）。
    quote 为 None 表示全部行情源失败（调用方计 error，不写快照）。
    """
    if symbol.endswith((".SS", ".SZ")):
        try:
            yahoo = quotes.fetch_yahoo(symbol)
        except quotes.QuoteError:
            return None, False, False
        try:
            tencent = quotes.fetch_tencent(symbol)
        except quotes.QuoteError:
            return yahoo, False, True
        if abs(yahoo["pct"] - tencent["pct"]) > TOLERANCE_PP:
            return yahoo, True, False
        return yahoo, False, False
    try:
        return quotes.fetch(symbol), False, False
    except quotes.QuoteError:
        return None, False, False


def run_cycle(config, conn, symbols, dry_run=False, workers=10):
    """跑一轮轮询。返回 summary dict（同时写快照、预警、心跳）。"""
    started = time.time()
    now = datetime.now(BEIJING)
    ts = now.isoformat()
    sym_cfg = config.get("thresholds", {}).get("symbols", {})
    summary = {"ts": ts, "checked": 0, "snapshots": 0, "triggers": 0,
               "alerts_sent": 0, "dry_run_alerts": 0, "blocked": {},
               "disagreements": [], "errors": []}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fetched = list(pool.map(fetch_symbol, symbols))
    for symbol, (quote, disagreement, backup_missing) in zip(symbols,
                                                             fetched):
        cfg = sym_cfg.get(symbol)
        if cfg is None:
            summary["errors"].append(f"{symbol}: 无阈值配置")
            continue
        summary["checked"] += 1
        if quote is None:
            summary["errors"].append(f"{symbol}: 行情全失败")
            continue
        name = cfg.get("name", symbol)
        if cfg.get("unit") == "bp":
            move = quotes.bp_change(quote["prev_close"], quote["price"])
            level = thresholds.classify(cfg, bp=move)
            pct_for_row = move  # bp 型标的：pct 列存基点变化，见 thresholds.classify
        else:
            move = quote["pct"]
            level = thresholds.classify(cfg, pct=move)
            pct_for_row = move
        if disagreement:
            level = "disagreement"
            summary["disagreements"].append(symbol)
        ledger_mod.record_snapshot(conn, ts, symbol, name, quote["price"],
                                   quote["prev_close"], pct_for_row, level,
                                   quote["source"])
        summary["snapshots"] += 1
        if level not in ("emergency", "regular"):
            continue
        summary["triggers"] += 1
        passed, blocked_by = gates.evaluate(config, conn, symbol, level, cfg,
                                            now)
        if not passed:
            summary["blocked"][blocked_by] = \
                summary["blocked"].get(blocked_by, 0) + 1
            continue
        if cfg.get("unit") == "bp":
            move_txt = f"{move:+.0f}bp（常规 ±{cfg['regular_bp']}bp / " \
                       f"紧急 ±{cfg['emergency_bp']}bp）"
        else:
            move_txt = f"{move:+.2f}%（常规 ±{cfg['regular']}% / " \
                       f"紧急 ±{cfg['emergency']}%）"
        title = f"异动提醒：{name}（{symbol}）{level}"
        content = f"现价 {quote['price']}，前收 {quote['prev_close']}，" \
                  f"变动 {move_txt}，来源 {quote['source']}。"
        result = push_mod.send_alert(config, title, content, dry_run=dry_run)
        is_dry = bool(result.get("dry_run"))
        ledger_mod.record_alert(conn, ts, symbol, level, pct_for_row, title,
                                pushed=0 if is_dry else 1,
                                dry_run=1 if is_dry else 0)
        if is_dry:
            summary["dry_run_alerts"] += 1
        else:
            summary["alerts_sent"] += 1
    summary["duration_s"] = round(time.time() - started, 1)
    status = "ok" if not summary["errors"] else "partial"
    detail = json.dumps(summary, ensure_ascii=False)
    ledger_mod.update_heartbeat(conn, ts, status, detail)
    hb_path = config.get("monitor", {}).get("heartbeat_file",
                                            "data/heartbeat.json")
    if not os.path.isabs(hb_path):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hb_path = os.path.join(root, hb_path)
    os.makedirs(os.path.dirname(hb_path), exist_ok=True)
    with open(hb_path, "w", encoding="utf-8") as f:
        json.dump({"last_run_ts": ts, "status": status, **summary}, f,
                  ensure_ascii=False, indent=2)
    return summary


def main():
    parser = argparse.ArgumentParser(description="监控轮询（单次循环）")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--config", default=os.path.join(root, "config.yaml"))
    parser.add_argument("--db", default=os.path.join(root, "data",
                                                     "ledger.db"))
    parser.add_argument("--symbols", default="",
                        help="逗号分隔的子集，用于测试；缺省全量")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    all_symbols = list(config.get("thresholds", {}).get("symbols", {}))
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] \
        or all_symbols
    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    conn = ledger_mod.init_db(args.db)
    summary = run_cycle(config, conn, symbols, dry_run=args.dry_run,
                        workers=args.workers)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
