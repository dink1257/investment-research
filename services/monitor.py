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
from functools import partial
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402
from core import gates, quotes, thresholds  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402
from push import push as push_mod  # noqa: E402

BEIJING = ZoneInfo("Asia/Shanghai")
TOLERANCE_PP = 0.5


def fetch_symbol(symbol, proxies=None):
    """拉取一标的。返回 (quote, disagreement, backup_missing)。

    A 股走双源：Yahoo 为主、腾讯为备，涨跌幅差 > 0.5pp 即不一致；
    非 A 股走主备链（Yahoo 优先，失败则腾讯——腾讯仅支持 A 股，
    故非 A 股实际只有 Yahoo 一源，失败即 QuoteError）。
    quote 为 None 表示全部行情源失败（调用方计 error，不写快照）。
    proxies 是 {原标的: 代理标的}（取自 config.yaml 的 proxy_symbol）：
    原标的取不到时改拉代理，quote 里带 "proxy" 字段；代理是 A 股时
    同样走双源校验。
    """
    return quotes.fetch_validated(symbol, proxies)


def run_cycle(config, conn, symbols, dry_run=False, workers=10):
    """跑一轮轮询。返回 summary dict（同时写快照、预警、心跳）。"""
    started = time.time()
    now = datetime.now(BEIJING)
    ts = now.isoformat()
    sym_cfg = config.get("thresholds", {}).get("symbols", {})
    proxies = {s: cfg["proxy_symbol"] for s, cfg in sym_cfg.items()
               if cfg.get("proxy_symbol")}
    summary = {"ts": ts, "checked": 0, "snapshots": 0, "triggers": 0,
               "alerts_sent": 0, "dry_run_alerts": 0, "blocked": {},
               "disagreements": [], "errors": []}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fetched = list(pool.map(
            partial(fetch_symbol, proxies=proxies), symbols))
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
        if quote.get("proxy"):
            content += f"（行情取自代理 {quote['proxy']}，原标的暂无数据）"
        result = push_mod.send_alert(config, title, content, level=level,
                                     dry_run=dry_run)
        is_dry = bool(result.get("dry_run"))
        delivered = bool(result.get("delivered"))
        ledger_mod.record_alert(conn, ts, symbol, level, pct_for_row, title,
                                pushed=1 if delivered else 0,
                                dry_run=1 if is_dry else 0)
        if is_dry:
            summary["dry_run_alerts"] += 1
        elif delivered:
            summary["alerts_sent"] += 1
        else:
            summary["errors"].append(f"{symbol}: 推送全部失败（{level}）")
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


def is_total_failure(summary):
    """退出码方案 C（用户 2026-11-08 定）：只在“全部失败”时返回非零。

    全部失败 = 一条行情快照都没写出来（全部行情源失败），
    或有预警放行但一条都没送达（推送全部失败）。
    部分失败仍返回 0，业务细节见心跳（status=partial + detail）。
    dry-run 下放行的预警计 dry_run_alerts，不算推送失败。
    """
    if summary["checked"] > 0 and summary["snapshots"] == 0:
        return True
    passed_gates = summary["triggers"] - sum(summary["blocked"].values())
    if (passed_gates > 0 and summary["alerts_sent"] == 0
            and summary["dry_run_alerts"] == 0):
        return True
    return False


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
    if is_total_failure(summary):
        sys.exit(1)


if __name__ == "__main__":
    main()
