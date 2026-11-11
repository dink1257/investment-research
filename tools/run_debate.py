#!/usr/bin/env python3
"""主题辩论 CLI（方案四 GitHub 腿：行业/政策问题）。

用法：
    python3 tools/run_debate.py --topic "人形机器人板块近期大涨能否持续" \\
        [--task-id T-123] [--budget-cny 0.5] [--deadline-utc 2026-11-11T10:00:00Z]
        [--config config.yaml] [--db data/ledger.db]
        [--quiet] [--run-id 123] [--attempt 1]

A（2026-11-11 隐私方案，E' 的修订）：Hermes 派发的是一个 Fernet 密文
payload（JSON {"topic": ...}），用 --payload-encrypted 传，密钥从环境
变量 HERMES_INPUT_KEY 读，解密只在内存里做、明文绝不打印；解密失败
写 failed 文件、不调 LLM。--quiet 供 workflow 用：结论只写 state 文件，
不打印到运行日志（运行日志是公开的）。--topic 明文保留，供本地/手动跑。
--run-id / --attempt 写入结果文件，重跑同一 task_id 时靠它区分。

流程：deadline 开始前检查（过了 deadline 直接写 blocked，不调 LLM）->
预算检查（与个股辩论共用日配额 8/天与月度熔断；同主题 2 小时去重）->
哨兵账本取行业动态 -> 调 LLM（6000 上限）-> 校验 -> 写账本 + 成本 ->
写 state/last_analysis.json + state/last_analysis.md -> 打印结论。
契约（Hermes 2026-11-10 反馈）：成功、阻塞、失败**都**写结果文件
（status 成功/阻塞/失败），开跑先写 running；Hermes 轮询靠 task_id + status
判断本轮状态，不用等到 6 分钟超时。budget_cny 只作声明记录，
实际费用以 cost_cny 为准；限额仍走账本已有的日配额与月度熔断，不另设。
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402
from agents import debate as debate_mod  # noqa: E402
from ledger import ledger as ledger_mod  # noqa: E402
from tools import input_crypto as input_crypto_mod  # noqa: E402
from tools import last_analysis as la_mod  # noqa: E402

BEIJING = ZoneInfo("Asia/Shanghai")


def _parse_run_id(value):
    """--run-id：GitHub run_id 是数字，写进 payload 时转 int；留空为 None。"""
    if not value:
        return None
    return int(value) if value.isdigit() else value


def parse_budget(value):
    """解析 --budget-cny：空串/缺省为 None（workflow 传空串时不报错）。"""
    if value in (None, ""):
        return None
    return float(value)


def parse_deadline(value):
    """解析 deadline_utc（ISO 8601，接受 Z 后缀；无时区按 UTC 计）。

    解析失败返回 None（只打印警告、不直接拦截，避免一个拼写错误
    堵住整次派发）；调用方自行决定。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def render_markdown(topic, verdict, cost_rmb):
    """辩论结论写成 Markdown（与 last_analysis.json 配对，供人直接读）。"""
    lines = [
        f"# 辩论研判（主题）：{topic}",
        "",
        f"- 结论：{verdict.get('verdict')}",
        f"- 信心：{verdict.get('confidence')}/5"
        + ("（已降为观望）" if verdict.get("downgraded") else ""),
        f"- 目标价区间：{verdict.get('target_range')}；"
        f"建议仓位：{verdict.get('position')}；"
        f"止损/失效条件：{verdict.get('stop_loss')}",
        f"- 本次成本：¥{cost_rmb}",
        "",
        "## 多方",
        str(verdict.get("bull", "")),
        "",
        "## 空方",
        str(verdict.get("bear", "")),
        "",
        "## 多方反驳",
        str(verdict.get("bull_rebuttal", "")),
        "",
        "## 空方反驳",
        str(verdict.get("bear_rebuttal", "")),
        "",
        "## 不确定项与证伪条件",
        str(verdict.get("uncertain", "")),
        "",
    ]
    return "\n".join(lines)


def render_terminal_note(topic, status, reason, cost_rmb=0.0):
    """阻塞/失败时也写一份短 Markdown，让 state/last_analysis.md 不过期。"""
    title = "辩论被拦截" if status == "blocked" else "辩论失败"
    return "\n".join([
        f"# {title}（主题）：{topic}",
        "",
        f"- 状态：{status}",
        f"- 原因：{reason}",
        f"- 本次成本：¥{cost_rmb}",
        "",
    ])


def main():
    parser = argparse.ArgumentParser(description="主题辩论（一次）")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--config", default=os.path.join(root, "config.yaml"))
    parser.add_argument("--db", default=os.path.join(root, "data",
                                                     "ledger.db"))
    parser.add_argument("--topic", default="",
                        help="行业/政策问题（例如：人形机器人板块近期大涨能否持续）")
    parser.add_argument("--payload-encrypted", default="",
                        help="Fernet 密文版 payload：JSON {\"topic\": ...}"
                             "（Hermes 派发用；与 --topic 二选一，"
                             "给了就优先用它，密钥从 HERMES_INPUT_KEY 读）")
    parser.add_argument("--quiet", action="store_true",
                        help="不把 Markdown 结论打印到 stdout（workflow 用："
                             "运行日志是公开的，结论只写 state 文件）")
    parser.add_argument("--run-id", default="",
                        help="GitHub 那次运行的编号（workflow 传 github.run_id）")
    parser.add_argument("--attempt", type=int, default=None,
                        help="第几次尝试（workflow 传 github.run_attempt）")
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
    data_dir = os.path.dirname(args.db)
    os.makedirs(data_dir, exist_ok=True)
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
            task_id, "debate", "deadline_exceeded", topic=args.topic,
            ts=ts, **common)
        la_mod.write_last_analysis(state_dir, payload)
        md = render_terminal_note(args.topic, "blocked",
                                  f"已过 deadline {args.deadline_utc}，未调 LLM")
        with open(os.path.join(state_dir, "last_analysis.md"), "w",
                  encoding="utf-8") as f:
            f.write(md)
        print(f"辩论被拦截：deadline_exceeded（{args.deadline_utc}），"
              "未调 LLM，未花钱")
        return 2
    topic = args.topic
    if args.payload_encrypted:
        try:
            payload = input_crypto_mod.decrypt_payload(args.payload_encrypted)
            topic = payload.get("topic") or ""
            if not topic:
                raise input_crypto_mod.InputCryptoError("decrypt_failed")
        except input_crypto_mod.InputCryptoError as exc:
            payload = la_mod.failed_payload(
                task_id, "debate", str(exc), "输入解密失败，未调 LLM，未花钱",
                0, topic=None,
                ts=datetime.now(BEIJING).isoformat(), **common)
            la_mod.write_last_analysis(state_dir, payload)
            md = render_terminal_note("", "failed",
                                      "输入解密失败，未调 LLM，未花钱", 0)
            with open(os.path.join(state_dir, "last_analysis.md"), "w",
                      encoding="utf-8") as f:
                f.write(md)
            print(f"辩论失败：{exc}，未调 LLM，未花钱")
            return 1
    if not topic:
        parser.error("必须提供 --topic 或 --payload-encrypted")
    la_mod.write_last_analysis(
        state_dir, la_mod.running_payload(task_id, "debate",
                                          topic=topic, ts=ts, **common))
    conn = ledger_mod.init_db(args.db)
    res = debate_mod.run_topic_debate(conn, config, topic, now=now)
    if res.get("blocked"):
        payload = la_mod.blocked_payload(
            task_id, "debate", res["blocked"], topic=topic,
            ts=datetime.now(BEIJING).isoformat(), **common)
        la_mod.write_last_analysis(state_dir, payload)
        md = render_terminal_note(topic, "blocked", res["blocked"])
        with open(os.path.join(state_dir, "last_analysis.md"), "w",
                  encoding="utf-8") as f:
            f.write(md)
        print(f"辩论被拦截：{res['blocked']}（未调 LLM，未花钱）")
        return 2
    if res.get("error"):
        cost = res.get("cost_rmb", 0)
        payload = la_mod.failed_payload(
            task_id, "debate", res["error"], res.get("detail", ""),
            cost, topic=topic,
            ts=datetime.now(BEIJING).isoformat(), **common)
        la_mod.write_last_analysis(state_dir, payload)
        md = render_terminal_note(topic, "failed",
                                  f"{res.get('error')} {res.get('detail', '')}",
                                  cost)
        with open(os.path.join(state_dir, "last_analysis.md"), "w",
                  encoding="utf-8") as f:
            f.write(md)
        print(f"辩论失败：{res.get('error')} {res.get('detail', '')}，"
              f"已记账，成本 ¥{cost}")
        return 1
    payload = la_mod.debate_payload(
        topic, res["verdict"], res["cost_rmb"],
        datetime.now(BEIJING).isoformat(), task_id=task_id,
        budget_cny=args.budget_cny,
        deadline_utc=args.deadline_utc or None,
        run_id=common["run_id"], attempt=common["attempt"])
    la_mod.write_last_analysis(state_dir, payload)
    md = render_markdown(topic, res["verdict"], res["cost_rmb"])
    with open(os.path.join(state_dir, "last_analysis.md"), "w",
              encoding="utf-8") as f:
        f.write(md)
    if not args.quiet:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
