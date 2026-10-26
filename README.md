# 投资研究系统

单体 Python 系统：自研监控层发现异动，新闻哨兵盯公告与重大新闻，
Supervisor 编排辩论与分析模块，所有结论经研判账本存档后推送。
LLM 只做推理和叙事，不算数字、不下单；月成本 ≤ ¥20（熔断写进代码）。

## 结构

```
core/       监控层：行情主备链、阈值引擎（纯计算，零 LLM 依赖）
agents/     辩论、分析师、宏观、综合（第 2 周）
ledger/     研判账本 SQLite + 预算账本
push/       飞书主通道 + Telegram 备通道（failover，不重复投递）
third_party/ MIT/Apache 抄入代码（各带 LICENSE/NOTICE）
services/   隔离服务（本期无对象）
tests/      单元测试、回归集、冒烟测试
tools/      gen_config.py（由实测 JSON 生成 config.yaml）
data/       账本、心跳（运行时生成，不进版本库）
```

## 快速开始

```bash
python3 tools/gen_config.py   # 由阈值实测 JSON 生成 config.yaml
python3 tests/test_thresholds.py
python3 tests/smoke_test.py   # 账本读写 + Yahoo 实测（需联网）
```

## 配置

`config.yaml` 由 `tools/gen_config.py` 生成，含 65 只标的自适应阈值
（过去 252 交易日 p95/p99，2026-10-26 实测，每月滚动刷新）。
**config 里不放任何密钥**：飞书 webhook、Telegram token 留空，部署时填。

## 路线图

- 第 1 周：核心监控（多源 failover + 自适应阈值 + 账本）+ 推送 + 心跳看板
- 第 2 周：辩论 + 深研 + 新闻哨兵 + 预算熔断
- 第 3 周：宏观 + 复盘 + 判定链 + 交易日历
- 第 4–5 周：影子运行 2 周（验收：漏跑 0、错报 0）
- 第 6–7 周：小范围推送 1–2 周

方案文档《投资研究系统-实施前方案审核》与全过程档案《投资研究系统-项目档案》
随项目文档另行保存（不在本仓库内）。
