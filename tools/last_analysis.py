"""方案四 GitHub 腿的结果文件：state/last_analysis.json。

Hermes（A 路径：轮询）靠这个文件拿 GitHub 腿的结论，不用解析日志或账本。
文件每次派发跑完就重写（只保留最新一次），且**所有终态都写**：
成功写 success，预算/去重拦截写 blocked，解析失败等写 failed，
跑起来先写 running —— 这样 Hermes 轮询时靠 task_id + status 就能区分
“还在跑”“已失败”和“上一轮的旧文件”，不用等到 6 分钟超时。

契约（Hermes 2026-11-10 反馈后定稿，见 source:45）：
- 路径 state/last_analysis.json（不用 data/，避开推送时误带账本数据的坑）；
  报告正文配对写 state/last_analysis.md（report_path 指向它），供人直接读。
- 派发输入：task_id、mode（deepdive/debate）、symbol（深研）、topic（辩论）、
  brief（深研的问题）、budget_cny（声明预算）、deadline_utc（开始前检查）。
- 字段：task_id、mode、status、type、symbol/topic、title、verdict、
  confidence（深研不产置信度，写 null，不编）、report_path、report、
  key_points[]（归集用的短要点）、falsify[]（证伪条件）、cost_cny（实花）、
  budget_cny（声明）、deadline_utc、finished_at。cost_rmb 与 ts 是旧字段的
  同值别名，留着给旧测试/旧账本对照，不再新增使用。
- run_id / attempt（E' 重做版新增）：GitHub 那次运行的编号与第几次尝试，
  重跑同一 task_id 时靠它区分；手动跑/未传时写 null。
"""
import json
import os

SCHEMA_VERSION = 2
REPORT_PATH = "state/last_analysis.md"


def _as_list(value):
    """uncertain 可能是 list / str / None，统一成去重后的字符串 list。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
        return out
    return [str(value)]


def _base(task_id, mode, status, finished_at, budget_cny=None,
          deadline_utc=None, run_id=None, attempt=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "mode": mode,
        "status": status,
        "type": mode,
        "title": "",
        "verdict": None,
        "confidence": None,
        "report_path": REPORT_PATH,
        "report": None,
        "key_points": [],
        "falsify": [],
        "cost_cny": 0.0,
        "cost_rmb": 0.0,
        "budget_cny": budget_cny,
        "deadline_utc": deadline_utc,
        "finished_at": finished_at,
        "ts": finished_at,
        "run_id": run_id,
        "attempt": attempt,
    }


def write_last_analysis(state_dir, payload):
    """写 state/last_analysis.json，返回文件路径。"""
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "last_analysis.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def running_payload(task_id, mode, symbol=None, topic=None, ts="",
                    budget_cny=None, deadline_utc=None, run_id=None,
                    attempt=None):
    """开跑时先写：Hermes 轮询看到本轮 task_id + running，就知道不是旧文件。"""
    payload = _base(task_id, mode, "running", ts, budget_cny, deadline_utc,
                    run_id, attempt)
    payload["symbol"] = symbol
    payload["topic"] = topic
    payload["title"] = ("深研进行中" if mode == "deepdive" else "辩论进行中")
    return payload


def blocked_payload(task_id, mode, reason, symbol=None, topic=None, ts="",
                    budget_cny=None, deadline_utc=None, run_id=None,
                    attempt=None):
    """拦截（预算/去重/月度熔断/超时 deadline）也写文件：轮询可立刻停。"""
    payload = _base(task_id, mode, "blocked", ts, budget_cny, deadline_utc,
                    run_id, attempt)
    payload["symbol"] = symbol
    payload["topic"] = topic
    payload["title"] = ("深研被拦截" if mode == "deepdive" else "辩论被拦截")
    payload["blocked"] = reason
    return payload


def failed_payload(task_id, mode, error, detail="", cost_cny=0.0,
                   symbol=None, topic=None, ts="", budget_cny=None,
                   deadline_utc=None, run_id=None, attempt=None):
    """失败（解析失败、解密失败等）也写文件，带已花费用：轮询可立刻停。"""
    payload = _base(task_id, mode, "failed", ts, budget_cny, deadline_utc,
                    run_id, attempt)
    payload["symbol"] = symbol
    payload["topic"] = topic
    payload["title"] = ("深研失败" if mode == "deepdive" else "辩论失败")
    payload["error"] = error
    payload["detail"] = detail
    payload["cost_cny"] = cost_cny
    payload["cost_rmb"] = cost_cny
    return payload


def deepdive_payload(symbol, name, report, cost_cny, ts, task_id=None,
                     budget_cny=None, deadline_utc=None, run_id=None,
                     attempt=None):
    """深研结论的结果文件体。深研不产置信度，confidence 写 null。"""
    payload = _base(task_id, "deepdive", "success", ts, budget_cny,
                    deadline_utc, run_id, attempt)
    points = [str(report.get("verdict"))] if report.get("verdict") else []
    levels = "; ".join(
        f"{k}：{report[k]}" for k in ("buy_point", "sell_point")
        if report.get(k))
    if levels:
        points.append(levels)
    if report.get("risk_strategy"):
        points.append(f"风险策略：{report['risk_strategy']}")
    payload.update({
        "symbol": symbol,
        "name": name,
        "title": f"深研报告：{name}（{symbol}）",
        "verdict": report.get("verdict"),
        "confidence": None,
        "report": report,
        "key_points": points[:5],
        "falsify": _as_list(report.get("uncertain")),
        "cost_cny": cost_cny,
        "cost_rmb": cost_cny,
    })
    return payload


def debate_payload(topic, verdict, cost_cny, ts, task_id=None,
                   budget_cny=None, deadline_utc=None, run_id=None,
                   attempt=None):
    """主题辩论结论的结果文件体。symbol 用主题键（与账本去重键一致）。"""
    payload = _base(task_id, "debate", "success", ts, budget_cny,
                    deadline_utc, run_id, attempt)
    points = [str(verdict.get("verdict"))] if verdict.get("verdict") else []
    levels = "; ".join(
        f"{k}：{verdict[k]}" for k, label in
        (("target_range", "目标价区间"), ("position", "建议仓位"),
         ("stop_loss", "止损/失效条件")) if verdict.get(k))
    if levels:
        points.append(levels)
    for side in ("bull", "bear"):
        if verdict.get(side):
            points.append(str(verdict[side]))
    falsify = _as_list(verdict.get("uncertain"))
    if verdict.get("stop_loss") and verdict["stop_loss"] not in falsify:
        falsify.append(str(verdict["stop_loss"]))
    payload.update({
        "symbol": "topic:" + topic[:60],
        "topic": topic,
        "title": f"辩论研判（主题）：{topic}",
        "verdict": verdict.get("verdict"),
        "confidence": verdict.get("confidence"),
        "report": verdict,
        "key_points": points[:5],
        "falsify": falsify,
        "cost_cny": cost_cny,
        "cost_rmb": cost_cny,
    })
    return payload
