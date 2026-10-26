# THIRD-PARTY清单

每段外来代码/设计的来源、许可证与用途。抄入代码时记录 commit 并保留 LICENSE/NOTICE；
只学设计的不抄代码；不采用的也记录原因，免得日后重复评估。

| 模块 | 来源 | 许可证 | 状态 | 用途 |
|---|---|---|---|---|
| 辩论协议（多空+裁判） | TradingAgents | Apache-2.0 | 待抄入（第 2 周） | 需求 2 的辩论流程 |
| 分析师分工 + 回测 | ai-hedge-fund | MIT | 待抄入（第 2 周） | 需求 3 的分析师与回测校验 |
| 确定性计算 + 叙事分离模式 | FinRobot | Apache-2.0 | 模式借鉴 + 部分代码抄入（第 2 周） | 数字与叙事分离 |
| 主备链、判定链、条件引擎、交易日历、可观测性 | PanWatch（commit ec3e87ea843c2da8b2863aaf64bfb17718d1cab1） | MIT | 设计借鉴；若抄代码则补 commit + LICENSE | 监控加固（8.5 分方案） |
| 公告/行情/事件数据端点 | AkShare、a-stock-data | 以其仓库为准，接入时核对 | 数据端点（非抄入代码） | 哨兵与行情备源，不进 third_party |
| 监控层、阈值引擎思路、飞书推送、前收推导 | stock-alerts（自有） | 自有 | 已复用 | 监控层基础；前收推导逻辑来自 prevclose patch（日K推导，替代不可靠的 meta 字段） |
| TrendRadar | TrendRadar | GPL-3.0 | 不采用（2026-10-26 已确认） | 热榜关键词机制覆盖不了公告级重大信息，无重大性分级与异动联动 |
| OpenStock / richfolio / TradingAgents-CN | AGPL-3.0 / 无许可文件 / 混合许可 | 设计参考 only | 不抄代码 | 仪表盘设计 / 成品形态思路 / 复盘模块设计 |

规则：抄 MIT/Apache，隔离 GPL，绕开无许可；已抄入版本按原许可继续可用；
新版本改许可则重新评估；每月检查、季度合并（见项目档案第 8 节）。
