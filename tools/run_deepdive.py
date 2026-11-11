#!/usr/bin/env python3
"""按需深研 CLI（方案 §3.2：你点名触发）。

用法：
    python3 tools/run_deepdive.py --symbol 300750.SZ \\
        --brief "Q3 财报后股价异动，想知道基本面有没有变坏"
        [--task-id T-123] [--budget-cny 0.5] [--deadline-utc 2026-11-11T10:00:00Z]
        [--out 深研-宁德时代.md] [--config config.yaml] [--db data/ledger.db]
        [--quiet] [--run-id 123] [--attempt 1]

A（2026-11-11 隐私方案，E' 的修订）：Hermes 派发的是一个 Fernet 密文
payload（JSON {"symbol": ..., "brief": ...}），用 --payload-encrypted 传，
密钥从环境变量 HERMES_INPUT_KEY 读，解密只在内存里做、明文绝不打印；
解密失败写 failed 文件、不调 LLM。标的代码也在密文里，不走明文输入。
--quiet 供 workflow 用：报告只写 state 文件，不打印到运行日志
（运行日志是公开的）。--symbol / --brief 明文保留，供本地/手动跑。
--run-id / --attempt 写入结果文件，重跑同一 task_id 时靠它区分。

流程：deadline 开始前检查（过了 deadline 直接写 blocked，不调 LLM）->
预算检查（≤2/天；月累计 ≥¥16 暂停）-> 调 LLM（8000 上限）->
回测校验买卖点 -> 写账本 + 成本 -> 写 state/last_analysis.json +
state/last_analysis.md（方案四结果文件，供 Hermes 轮询）-> 打印 Markdown 报告
（--out 时同时写文件）。
契约（Hermes 2026-11-10 反馈）：成功、阻塞、失败**都**写结果文件
（status 成功/阻塞/失败），开跑先写 running；Hermes 轮询靠 task_id + status
判断本轮状态，不用等到 6 分钟超时。budget_cny 只作声明记录，
实际费用以 cost_cny 为准；限额仍走账本已有的日配额与月度熔断，不另设。
深研是用户点名才跑的动作，没有 dry-run：跑一次约 ¥0.13，记账是审计链的一环，
跳过记账等于断了复盘依据。测试请用 mock 的 llm_call（见 tests/test_week2.py）。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402
from agents import deepdive as deepdive_mod  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402
from tools import input_crypto as input_crypto_mod  # noqa: E402
from tools import last_analysis as la_mod  # noqa: E402
from tools.run_debate import parse_budget, parse_deadline  # noqa: E402

BEIJING = ZoneInfo("Asia/Shanghai")


def _parse_run_id(value):
    """--run-id：GitHub run_id 是数字，写进 payload 时转 int；留空为 None。"""
    if not value:
        return None
    return int(value) if value.isdigit() else value


def render_markdown(symbol, name, report, cost_rmb):
    """深研结论写成 Markdown 报告（方案 §4 输出 4：文档 + 账本）。"""
    backtest = report.get("backtest") or {}
    lines = [
        f"# 深研报告：{name}（{symbol}）",
        "",
        f"- 结论：{report.get('verdict')}",
        f"- 买点：{report.get('buy_point')}；卖点：{report.get('sell_point')}",
        f"- 风险策略：{report.get('risk_strategy')}",
        f"- 回测校验：{json.dumps(backtest, ensure_ascii=False)}",
        f"- 本次成本：¥{cost_rmb}",
        "",
        "## 基本面",
        str(report.get("fundamental", "")),
        "",
        "## 技术面",
        str(report.get("technical", "")),
        "",
        "## 资金面",
        str(report.get("capital", "")),
        "",
        "## 风险（只挑毛病）",
        str(report.get("risks", "")),
        "",
        "## 不确定项与证伪条件",
        str(report.get("uncertain", "")),
        "",
    ]
    return "\n".join(lines)


def render_terminal_note(symbol, name, status, reason, cost_rmb=0.0):
    """阻塞/失败时也写一份短 Markdown，让 state/last_analysis.md 不过期。"""
    title = "深研被拦截" if status == "blocked" else "深研失败"
    return "\n".join([
        f"# {title}：{name}（{symbol}）",
        "",
        f"- 状态：{status}",
        f"- 原因：{reason}",
        f"- 本次成本：¥{cost_rmb}",
        "",
    ])


def main():
    parser = argparse.ArgumentParser(description="按需深研（一次）")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--config", default=os.path.join(root, "config.yaml"))
    parser.add_argument("--db", default=os.path.join(root, "data",
                                                     "ledger.db"))
    parser.add_argument("--symbol", default="",
                        help="标的代码（例如 300750.SZ；本地/手动跑用，"
                             "与 --payload-encrypted 二选一）")
    parser.add_argument("--brief", default="",
                        help="背景/问题（例如异动原因、财报要点）；留空则按常规框架全面分析")
    parser.add_argument("--payload-encrypted", default="",
                        help="Fernet 密文版 payload：JSON {\"symbol\": ..., "
                             "\"brief\": ...}（Hermes 派发用；与 --symbol 二选一，"
                             "给了就优先用它，密钥从 HERMES_INPUT_KEY 读）")
    parser.add_argument("--quiet", action="store_true",
                        help="不把 Markdown 报告打印到 stdout（workflow 用："
                             "运行日志是公开的，报告只写 state 文件）")
    parser.add_argument("--run-id", default="",
                        help="GitHub 那次运行的编号（workflow 传 github.run_id）")
    parser.add_argument("--attempt", type=int, default=None,
                        help="第几次尝试（workflow 传 github.run_attempt）")
    parser.add_argument("--out", default="",
                        help="同时把 Markdown 报告写到该文件")
    parser.add_argument("--task-id", default="",
                        help="Hermes 派发的 task_id（轮询靠它认本轮；手动跑可留空）")
    parser.add_argument("--budget-cny", type=parse_budget, default=None,
                        help="Hermes 声明的本轮预算（元，仅记录，不作限额依据）")
    parser.add_argument("--deadline-utc", default="",
                        help="Hermes 声明的截止时间（ISO 8601 UTC；过了则不跑）")
    parser.add_argument("--state-dir", default="",
                        help="结果文件目录（默认 <仓库根>/state；测试时可指向临时目录）")
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    sym_cfg = config.get("thresholds", {}).get("symbols", {})
    name = (sym_cfg.get(args.symbol) or {}).get("name", args.symbol)
    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    state_dir = args.state_dir or os.path.join(root, "state")
    task_id = args.task_id or None
    now = datetime.now(BEIJING)
    ts = now.isoformat()
    common = dict(budget_cny=args.budget_cny,
                  deadline_utc=args.deadline_utc or None,
                  run_id=_parse_run_id(args.run_id),
                  attempt=args.attempt)
    deadline = parse_deadline(args.deadline_utc)
    if args.deadline_utc and deadline is None:
        print(f"警告：deadline_utc 解析失败（{args.deadline_utc}），"
              "按无 deadline 继续跑")
    if deadline is not None and datetime.now(timezone.utc) > deadline:
        payload = la_mod.blocked_payload(
            task_id, "deepdive", "deadline_exceeded",
            symbol=args.symbol or None,
            ts=ts, **common)
        la_mod.write_last_analysis(state_dir, payload)
        md = render_terminal_note(args.symbol or "-",
                                  name or "未确认标的", "blocked",
                                  f"已过 deadline {args.deadline_utc}，未调 LLM")
        with open(os.path.join(state_dir, "last_analysis.md"), "w",
                  encoding="utf-8") as f:
            f.write(md)
        print(f"深研被拦截：deadline_exceeded（{args.deadline_utc}），"
              "未调 LLM，未花钱")
        return 2
    symbol = args.symbol
    brief = args.brief
    if args.payload_encrypted:
        try:
            payload = input_crypto_mod.decrypt_payload(args.payload_encrypted)
            symbol = (payload.get("symbol") or "").strip()
            brief = payload.get("brief") or ""
            if not symbol:
                raise input_crypto_mod.InputCryptoError("decrypt_failed")
        except input_crypto_mod.InputCryptoError as exc:
            payload = la_mod.failed_payload(
                task_id, "deepdive", str(exc), "输入解密失败，未调 LLM，未花钱",
                0, symbol=None,
                ts=datetime.now(BEIJING).isoformat(), **common)
            la_mod.write_last_analysis(state_dir, payload)
            md = render_terminal_note("-", "未确认标的", "failed",
                                      "输入解密失败，未调 LLM，未花钱", 0)
            with open(os.path.join(state_dir, "last_analysis.md"), "w",
                      encoding="utf-8") as f:
                f.write(md)
            print(f"深研失败：{exc}，未调 LLM，未花钱")
            return 1
    if not symbol:
        parser.error("必须提供 --symbol 或 --payload-encrypted")
    name = (sym_cfg.get(symbol) or {}).get("name", symbol)
    la_mod.write_last_analysis(
        state_dir, la_mod.running_payload(task_id, "deepdive",
                                          symbol=symbol, ts=ts, **common))
    conn = ledger_mod.init_db(args.db)
    res = deepdive_mod.run_deepdive(conn, config, symbol, name,
                                    brief or
                                    "用户点名深研，未给具体问题，按常规框架全面分析。",
                                    now=now)
    if res.get("blocked"):
        payload = la_mod.blocked_payload(
            task_id, "deepdive", res["blocked"], symbol=symbol,
            ts=datetime.now(BEIJING).isoformat(), **common)
        la_mod.write_last_analysis(state_dir, payload)
        md = render_terminal_note(symbol, name, "blocked",
                                  res["blocked"])
        with open(os.path.join(state_dir, "last_analysis.md"), "w",
                  encoding="utf-8") as f:
            f.write(md)
        print(f"深研被拦截：{res['blocked']}（未调 LLM，未花钱）")
        return 2
    if res.get("error"):
        cost = res.get("cost_rmb", 0)
        payload = la_mod.failed_payload(
            task_id, "deepdive", res["error"], res.get("detail", ""),
            cost, symbol=symbol,
            ts=datetime.now(BEIJING).isoformat(), **common)
        la_mod.write_last_analysis(state_dir, payload)
        md = render_terminal_note(symbol, name, "failed",
                                  f"{res.get('error')} {res.get('detail', '')}",
                                  cost)
        with open(os.path.join(state_dir, "last_analysis.md"), "w",
                  encoding="utf-8") as f:
            f.write(md)
        print(f"深研失败：{res.get('error')} {res.get('detail', '')}，"
              f"已记账，成本 ¥{cost}")
        return 1
    md = render_markdown(symbol, name, res["report"],
                         res["cost_rmb"])
    payload = la_mod.deepdive_payload(
        symbol, name, res["report"], res["cost_rmb"],
        datetime.now(BEIJING).isoformat(), task_id=task_id,
        budget_cny=args.budget_cny,
        deadline_utc=args.deadline_utc or None,
        run_id=common["run_id"], attempt=common["attempt"])
    la_mod.write_last_analysis(state_dir, payload)
    with open(os.path.join(state_dir, "last_analysis.md"), "w",
              encoding="utf-8") as f:
        f.write(md)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"报告已写到 {args.out}")
    if not args.quiet:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
