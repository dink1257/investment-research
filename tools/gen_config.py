#!/usr/bin/env python3
"""由阈值实测 JSON 生成 repo/config.yaml（可重复跑）。

输入：hidden_files/volatility/threshold_review.json（252 交易日实测，2026-10-26）
输出：repo/config.yaml。config 里不放任何密钥（webhook / token 留空，部署时填）。
"""
import json
import os

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 阈值数据优先用仓库自带的 tools/threshold_review.json（自包含），
# 本地开发时若无则回退到 hidden_files 的实测文件。
_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "threshold_review.json")
SRC = _LOCAL if os.path.exists(_LOCAL) else os.path.expanduser(
    "~/workspace/goals/investment-research-system/hidden_files/"
    "volatility/threshold_review.json")

DEFAULTS = {
    "A股个股": {"regular": 4.0, "emergency": 8.0},
    "港股个股": {"regular": 5.0, "emergency": 8.0},
    "美股个股": {"regular": 5.0, "emergency": 7.0},
    "A股指数": {"regular": 2.5, "emergency": 4.5},
    "港股指数": {"regular": 2.5, "emergency": 4.0},
    "美股指数": {"regular": 2.0, "emergency": 3.5},
    "黄金": {"regular": 3.0, "emergency": 4.0},
    "原油": {"regular": 5.0, "emergency": 9.0},
    "外汇": {"regular": 0.5, "emergency": 1.2},
}


def main():
    data = json.load(open(SRC, encoding="utf-8"))
    # 2026-10-27：industry_keywords 是用户已定稿的内容（见 config.yaml），
    # 重新生成时必须保留，绝不能重置为空，否则会静默丢掉用户确认的关键词表。
    out = os.path.join(REPO, "config.yaml")
    kept_keywords = []
    if os.path.exists(out):
        try:
            existing = yaml.safe_load(open(out, encoding="utf-8")) or {}
            kept_keywords = (existing.get("sentinel") or {}).get(
                "industry_keywords") or []
        except Exception:
            kept_keywords = []
    symbols = {}
    for d in data:
        if "error" in d:
            continue
        if d["symbol"] == "^TNX":
            # 利率指标：用基点，不用百分比（百分比实测 p95 1.815% / p99 2.75% 仅供参考）
            symbols["^TNX"] = {
                "name": d["name"], "unit": "bp",
                "regular_bp": 10, "emergency_bp": 20,
                "note": "利率指标，用基点阈值",
            }
            continue
        entry = {"name": d["name"], "unit": "pct",
                 "regular": round(d["p95"], 2),
                 "emergency": round(d["p99"], 2),
                 "sigma": d["sigma"]}
        if "proxy" in d:
            entry["proxy"] = d["proxy"]
        symbols[d["symbol"]] = entry
    # CN=F（A50 期指）Yahoo 无数据：暂用沪深 300ETF 参考，实施时走 AkShare/腾讯源补算
    symbols["CN=F"] = {"name": "A50期指", "unit": "pct",
                       "regular": 2.37, "emergency": 3.07,
                       "proxy": "沪深300ETF（实施时走 AkShare/腾讯源补算）"}
    config = {
        "model": {"provider": "dashscope", "name": "qwen3.8-flash",
                  "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
        "budget": {"currency": "CNY", "monthly_cap": 20, "warn_at": 16,
                   "daily_debate_cap": 8, "daily_deepdive_cap": 2,
                   "dedup_hours": 2},
        "push": {"primary": "feishu", "backup": "telegram",
                 "feishu_webhook": "", "telegram_bot_token": "",
                 "telegram_chat_id": ""},
        "monitor": {"sources_priority": ["yahoo", "tencent"],
                    "heartbeat_file": "data/heartbeat.json",
                    "poll_interval_minutes": 15},
        "thresholds": {
            "window": "过去 252 个交易日（截至 2026-10-26），每月滚动刷新",
            "defaults": DEFAULTS,
            "symbols": symbols,
        },
        "sentinel": {
            "sources": ["巨潮资讯公告", "上交所公告", "深交所公告",
                        "港交所披露易", "财联社电报", "华尔街见闻快讯"],
            "industry_keywords": kept_keywords,
            "grades": {
                "emergency": ["停牌", "立案调查", "业绩暴雷", "重大重组", "实控人变更"],
                "important": ["财报", "业绩预告", "减持", "增持", "回购", "问询函"],
                "routine": ["日常公告", "调研纪要"],
            },
        },
        "gate_chain": {"order": ["enabled", "not_expired", "trading_session",
                                 "daily_limit", "cooldown", "dedup",
                                 "quiet_hours"]},
    }
    with open(out, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)
    print(f"config.yaml 已生成，共 {len(symbols)} 只标的阈值 -> {out}")


if __name__ == "__main__":
    main()
