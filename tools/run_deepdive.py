#!/usr/bin/env python3
"""按需深研 CLI（方案 §3.2：你点名触发）。

用法：
    python3 tools/run_deepdive.py --symbol 300750.SZ \\
        --brief "Q3 财报后股价异动，想知道基本面有没有变坏"
        [--out 深研-宁德时代.md] [--config config.yaml] [--db data/ledger.db]

流程：预算检查（≤2/天；月累计 ≥¥16 暂停）-> 调 LLM（8000 上限）->
回测校验买卖点 -> 写账本 + 成本 -> 打印 Markdown 报告（--out 时同时写文件）。
深研是用户点名才跑的动作，没有 dry-run：跑一次约 ¥0.13，记账是审计链的一环，
跳过记账等于断了复盘依据。测试请用 mock 的 llm_call（见 tests/test_week2.py）。
"""
import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402
from agents import deepdive as deepdive_mod  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402

BEIJING = ZoneInfo("Asia/Shanghai")


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


def main():
    parser = argparse.ArgumentParser(description="按需深研（一次）")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--config", default=os.path.join(root, "config.yaml"))
    parser.add_argument("--db", default=os.path.join(root, "data",
                                                     "ledger.db"))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--brief", default="",
                        help="背景/问题（例如异动原因、财报要点）；留空则按常规框架全面分析")
    parser.add_argument("--out", default="",
                        help="同时把 Markdown 报告写到该文件")
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    sym_cfg = config.get("thresholds", {}).get("symbols", {})
    name = (sym_cfg.get(args.symbol) or {}).get("name", args.symbol)
    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    conn = ledger_mod.init_db(args.db)
    now = datetime.now(BEIJING)
    res = deepdive_mod.run_deepdive(conn, config, args.symbol, name,
                                    args.brief or
                                    "用户点名深研，未给具体问题，按常规框架全面分析。",
                                    now=now)
    if res.get("blocked"):
        print(f"深研被拦截：{res['blocked']}（未调 LLM，未花钱）")
        return 2
    if res.get("error"):
        print(f"深研失败：{res.get('error')} {res.get('detail', '')}，"
              f"已记账，成本 ¥{res.get('cost_rmb', 0)}")
        return 1
    md = render_markdown(args.symbol, name, res["report"],
                         res["cost_rmb"])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"报告已写到 {args.out}")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
