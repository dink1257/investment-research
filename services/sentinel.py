"""新闻哨兵（方案 §3.6）：匹配 + 规则分级 + 去重 + 异动联动 + 推送。

- 匹配：watchlist 股票代码 + 简称 + 行业关键词表（配置已定稿 6 组 33 词）。
  代码/简称命中归属到标的；仅关键词命中记为行业项（symbol=''，标题加 [行业] 前缀）。
- 分级（规则先行，LLM 只做摘要）：用配置 grades 表——紧急立即推、
  重要摘要推、一般只记账本不推。
- 去重：同标的 + 标题相似 + 24 小时窗口，只推一次（标题归一化后
  完全一致或 difflib 相似度 ≥0.85 判为同一事件）。
- 联动：监控流先查哨兵账本（近 7 天）再搜因；debate.get_cause_brief 已接。
- 抓取层（AkShare）：2026-10-27 在本环境实测，东财与巨潮 host 均超时
  （curl 28 / 120 秒无响应），故抓取函数全部 try/except 降级：
  取不到只记源状态、不造假条目；部署环境（Actions）网络不同，到时再验 live。
  连续 2 天取不到数据的源触发一次告警（方案 §3.6 维护对策）。
"""
import difflib
import json
import os
import re
import string
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agents import llm as llm_mod
from core import budget as budget_mod
from ledger import ledger as ledger_mod
from push import push as push_mod

BEIJING = ZoneInfo("Asia/Shanghai")
DEDUP_HOURS = 24
SIMILARITY = 0.85


def grade_title(title, grades):
    """规则分级：标题含紧急词 -> emergency，含重要词 -> important，否则 routine。"""
    for term in grades.get("emergency", []):
        if term and term in title:
            return "emergency"
    for term in grades.get("important", []):
        if term and term in title:
            return "important"
    return "routine"


def _code_of(symbol):
    if symbol.endswith((".SS", ".SZ")):
        return symbol[:-3]
    if symbol.endswith(".HK"):
        return symbol[:-3]
    return symbol


def match_item(item, symbols_cfg, keyword_groups):
    """匹配一条新闻/公告。返回 (symbol, via, group)，symbol='' 表示行业项，
    via 为 code/name/keyword/None（None = 与 watchlist 无关，丢弃）。"""
    text = f"{item.get('title', '')} {item.get('content', '')}"
    title = item.get("title", "")
    for symbol, cfg in symbols_cfg.items():
        code = _code_of(symbol)
        if code and re.search(rf"(?<!\d){re.escape(code)}(?!\d)", title):
            return symbol, "code", None
        name = cfg.get("name", "")
        if name and name.lower() in title.lower():
            return symbol, "name", None
        if not symbol.endswith((".SS", ".SZ", ".HK")) and symbol.lower() \
                in title.lower():
            return symbol, "code", None
    for group in keyword_groups:
        for kw in group.get("keywords", []):
            if kw and kw in text:
                return "", "keyword", group.get("group", "")
    return None, None, None


def _normalize(title):
    drop = string.punctuation + "，。、；：！？《》「」『』【】()（） \t"
    return "".join(ch for ch in title if ch not in drop).lower()


def is_dup(conn, symbol, title, now):
    """同标的 24 小时内标题相似即判重复。"""
    since = (now - timedelta(hours=DEDUP_HOURS)).isoformat()
    rows = conn.execute(
        "SELECT title FROM sentinel_items WHERE symbol = ? AND ts >= ?",
        (symbol, since)).fetchall()
    norm = _normalize(title)
    for (old,) in rows:
        old_norm = _normalize(old)
        if norm and norm == old_norm:
            return True
        if norm and old_norm and difflib.SequenceMatcher(
                None, norm, old_norm).ratio() >= SIMILARITY:
            return True
    return False


def summarize(item, llm_call):
    """重要项摘要（≤100 字）。LLM 失败时调用方改推标题原文，不编造。"""
    prompt = (f"用不超过 100 字摘要这条新闻，只讲事实，不推测：\n"
              f"标题：{item.get('title', '')}\n"
              f"内容：{item.get('content', '')[:500]}")
    text, _usage, cost = llm_call(llm_mod.SYSTEM_PROMPT, prompt, 500)
    return llm_mod.strip_fence(text).strip(), cost


def process_items(conn, config, items, now, push_fn=None, llm_call=None):
    """处理一批已抓取项：匹配 -> 分级 -> 去重 -> 记账 -> 分级推送。
    返回计数字典。push_fn(config, title, content, dry_run) 缺省走 push 模块。"""
    symbols_cfg = config.get("thresholds", {}).get("symbols", {})
    groups = config.get("sentinel", {}).get("industry_keywords", [])
    grades = config.get("sentinel", {}).get("grades", {})
    push = push_fn or push_mod.send_alert
    counts = {"matched": 0, "pushed_emergency": 0, "pushed_important": 0,
              "ledger_only": 0, "skipped_unmatched": 0, "skipped_dup": 0}
    ts = now.isoformat()
    for item in items:
        symbol, _via, group = match_item(item, symbols_cfg, groups)
        if symbol is None:
            counts["skipped_unmatched"] += 1
            continue
        title = item.get("title", "")
        if group:
            title = f"[行业] {title}"
        if is_dup(conn, symbol, title, now):
            counts["skipped_dup"] += 1
            continue
        grade = grade_title(title, grades)
        row_id = ledger_mod.record_sentinel(
            conn, ts, symbol, item.get("source", ""), title, grade,
            item.get("url", ""), pushed=0)
        counts["matched"] += 1
        if grade == "emergency":
            push(config, f"哨兵紧急：{title}",
                 f"来源：{item.get('source', '')} {item.get('url', '')}",
                 dry_run=False)
            ledger_mod.mark_sentinel_pushed(conn, row_id)
            counts["pushed_emergency"] += 1
        elif grade == "important":
            content = f"来源：{item.get('source', '')} {item.get('url', '')}"
            if llm_call is not None:
                allowed, _r = budget_mod.check_summary(conn, config, now)
                if allowed:
                    try:
                        summary, cost = summarize(item, llm_call)
                        budget_mod.record_llm_cost(conn, now, llm_mod.MODEL,
                                                   cost)
                        content = f"{summary}\n{content}"
                    except Exception:  # noqa: BLE001 - 摘要失败改推标题原文
                        pass
            push(config, f"哨兵重要：{title}", content, dry_run=False)
            ledger_mod.mark_sentinel_pushed(conn, row_id)
            counts["pushed_important"] += 1
        else:
            counts["ledger_only"] += 1
    return counts


# ---------------- 抓取层（AkShare，可降级） ----------------

def _pick_row(row, *keywords):
    """在 DataFrame 行里按列名关键字 defensively 取值；取不到返回 ''。"""
    for col in row.index:
        for kw in keywords:
            if kw in str(col):
                val = row[col]
                return "" if val is None else str(val)
    return ""


def _rows_to_items(df, source):
    items = []
    for _, row in df.iterrows():
        title = _pick_row(row, "标题", "title")
        if not title:
            continue
        items.append({
            "title": title,
            "content": _pick_row(row, "内容", "摘要", "content"),
            "url": _pick_row(row, "链接", "url", "地址"),
            "source": source,
        })
    return items


def fetch_akshare_news(code6):
    """东财个股新闻（AkShare stock_news_em）。返回 (items, status)。
    本环境实测超时 -> status 为 unavailable，items 为空，不造假。"""
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol=code6)
        return _rows_to_items(df, "akshare/东财新闻"), "ok"
    except Exception as e:  # noqa: BLE001
        return [], f"unavailable: {e}"


def fetch_akshare_notices(code6, date_str):
    """个股公告（AkShare stock_notice_report，date 取 YYYYMMDD）。
    参数语义未 live 验证过，任何失败都降级为 unavailable。"""
    try:
        import akshare as ak
        df = ak.stock_notice_report(symbol=code6, date=date_str)
        return _rows_to_items(df, "akshare/公告"), "ok"
    except Exception as e:  # noqa: BLE001
        return [], f"unavailable: {e}"


def load_source_status(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_source_status(path, status):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def run_sentinel(conn, config, now, push_fn=None, llm_call=None,
                 fetch_fn=None, status_path=None):
    """跑一轮哨兵：抓取（可注入 mock）-> 处理 -> 源状态跟踪。
    源连续 2 天失败触发一次告警（之后不再重复告警，直到恢复）。
    返回 {"counts": ..., "sources": {name: status}}。"""
    push = push_fn or push_mod.send_alert
    if fetch_fn is None:
        # 未接抓取函数时不跑抓取（调用方显式传 fetch_fn 才抓取），
        # 绝不能把"没抓"记成"抓取失败"，否则次日即误触发 2 天告警。
        return {"counts": {"matched": 0, "pushed_emergency": 0,
                           "pushed_important": 0, "ledger_only": 0,
                           "skipped_unmatched": 0, "skipped_dup": 0},
                "sources": {}}
    symbols_cfg = config.get("thresholds", {}).get("symbols", {})
    status = load_source_status(status_path) if status_path else {}
    today = now.strftime("%Y-%m-%d")
    all_counts = {"matched": 0, "pushed_emergency": 0, "pushed_important": 0,
                  "ledger_only": 0, "skipped_unmatched": 0, "skipped_dup": 0}
    for symbol in symbols_cfg:
        items, src_status = fetch_fn(symbol)
        st = status.get(symbol, {"fail_days": 0, "alerted": False,
                                 "last_seen": ""})
        if src_status == "ok":
            st = {"fail_days": 0, "alerted": False, "last_seen": today}
        else:
            if st.get("last_seen") != today:
                st["fail_days"] = st.get("fail_days", 0) + 1
                st["last_seen"] = today
            if st["fail_days"] >= 2 and not st.get("alerted"):
                push(config, f"哨兵数据源告警：{symbol}",
                     f"抓取连续 {st['fail_days']} 天失败：{src_status}",
                     dry_run=False)
                st["alerted"] = True
        status[symbol] = st
        counts = process_items(conn, config, items, now, push_fn=push,
                               llm_call=llm_call)
        for key in all_counts:
            all_counts[key] += counts[key]
    if status_path:
        save_source_status(status_path, status)
    return {"counts": all_counts,
            "sources": {s: ("ok" if v.get("fail_days", 0) == 0 else "fail")
                        for s, v in status.items()}}
