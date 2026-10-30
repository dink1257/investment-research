"""确定性回测校验（深研买卖点用，零 LLM）：程序算数，不由 LLM 拍脑袋。

- fetch_closes(symbol)：Yahoo v8 chart 近 1 年日K收盘价。
- backtest_from_closes(closes, buy, sell)：给定买点/卖点数值，
  在过去 252 个交易日里：买点是否在价格区间内；收盘价触及买点的天数；
  每次触及后 20 个交易日的远期收益分布（中位数、负收益占比）；
  卖点在 20 日窗口内被触及的比例。买点/卖点解析不出（null 或无数字）
  时跳过校验并注明，不编造。
"""
import json
import re
import urllib.request


def parse_level(text):
    """从买点/卖点文本里取第一个数值；取不到返回 None（不猜数）。"""
    if text is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(text).replace("，", ","))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _median(vals):
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return None
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def backtest_from_closes(closes, buy, sell):
    """纯计算回测。closes 为从旧到新的收盘价序列。"""
    closes = [c for c in closes if c is not None][-252:]
    if len(closes) < 30:
        return {"skipped": "历史数据不足 30 日"}
    if buy is None:
        return {"skipped": "buy_point 无数值，无法校验"}
    current = closes[-1]
    lo, hi = min(closes), max(closes)
    result = {"buy_in_range": lo <= buy <= hi,
              "price_low": round(lo, 2), "price_high": round(hi, 2),
              "current": round(current, 2)}
    # 触及买点的交易日（买点在现价之下看向下触及，反之看向上触及）
    if buy <= current:
        triggers = [i for i, c in enumerate(closes) if c <= buy]
    else:
        triggers = [i for i, c in enumerate(closes) if c >= buy]
    usable = [i for i in triggers if i + 20 < len(closes)]
    result["touches"] = len(triggers)
    if not usable:
        result["note"] = "买点触及日无完整 20 日远期窗口"
        return result
    fwds = [(closes[i + 20] / closes[i] - 1.0) * 100.0 for i in usable]
    result["median_fwd_20d_pct"] = round(_median(fwds), 2)
    result["negative_share_pct"] = round(
        sum(1 for r in fwds if r < 0) / len(fwds) * 100, 1)
    if sell is not None and sell > buy:
        reached = 0
        for i in usable:
            window = closes[i + 1:i + 21]
            if window and max(window) >= sell:
                reached += 1
        result["sell_reached_pct"] = round(reached / len(usable) * 100, 1)
    return result


def fetch_closes(symbol):
    """Yahoo v8 近 1 年日K收盘价（query1→query2）。失败 raise ValueError。"""
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = (f"https://{host}/v8/finance/chart/{symbol}"
               "?range=1y&interval=1d")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            result = data["chart"]["result"][0]
            quote = (result.get("indicators", {}).get("quote") or [{}])[0]
            closes = [c for c in (quote.get("close") or []) if c]
            if closes:
                return closes
        except Exception:  # noqa: BLE001
            continue
    raise ValueError(f"回测行情拉取失败：{symbol}")
