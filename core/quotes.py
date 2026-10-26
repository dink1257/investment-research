"""行情主备链：Yahoo v8 为主，腾讯为备（仅 A 股），全失败则报错（绝不静默返回空）。

前收推导源自 stock-alerts prevclose patch 并经 2026-10-26 实测修正：
不用 meta.chartPreviousClose（2026-10-21 拓荆误报教训），改从日K蜡烛推导——
前收恒取倒数第二根（上一完整交易日），现价盘中取 meta 实时价、盘后取最后一根；
推导不出则跳过该标的，宁可漏报也不误报。
双源交叉校验：两源涨跌幅差 > 0.5 个百分点即判定不一致，只告警不触发。
"""
import json
import time
import urllib.request


class QuoteError(Exception):
    """全部行情源失败。"""


class DisagreementError(Exception):
    """双源涨跌幅不一致，须告警、不触发异动。"""


def prev_close_from_candles(chart_result):
    """从日K蜡烛序列推导前收（源自 stock-alerts prevclose patch，经 2026-10-26 实测修正）。

    2026-10-21 教训：chartPreviousClose 会返回错误值（拓荆科技误报 -15%，
    实际 -2.38%），且用同一字段"复核"不算独立验证——前收必须从日K推导。
    2026-10-26 修正：原 patch 的"盘后取最后一根作前收"在本环境失效——
    实测盘后 meta 价与最后一根收盘价完全相等（茅台 1335.0 = 1335.0），
    会导致盘后涨跌幅恒为 0%。改为前收恒取倒数第二根（上一完整交易日），
    现价盘中取 meta 实时价、盘后取最后一根收盘价；取不到则返回 None
    （调用方跳过该 symbol，宁可漏报也不误报）。
    """
    try:
        quote = (chart_result.get("indicators", {}).get("quote") or [{}])[0]
        closes = [c for c in (quote.get("close") or []) if c]
        if len(closes) < 2:
            return None
        return closes[-2]
    except Exception:  # noqa: BLE001
        return None


def _in_session(chart_result):
    """当前是否处于常规交易时段（meta.currentTradingPeriod.regular）。"""
    regular = (chart_result.get("meta", {}).get("currentTradingPeriod", {})
               .get("regular", {}))
    if not regular:
        return False
    now = time.time()
    return regular.get("start", 0) <= now <= regular.get("end", 0)


def _yahoo_chart(symbol):
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = (f"https://{host}/v8/finance/chart/{symbol}"
               "?range=5d&interval=1d")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            result = data["chart"]["result"][0]
            return result
        except Exception:  # noqa: BLE001
            continue
    return None


def fetch_yahoo(symbol):
    """Yahoo v8：返回 {symbol, price, prev_close, pct, source}。

    现价：盘中取 meta 实时价，盘后/盘前取最后一根收盘价；
    前收：恒取倒数第二根（上一完整交易日）；涨跌幅本地重算。
    """
    result = _yahoo_chart(symbol)
    if result is None:
        raise QuoteError(f"Yahoo 拉取失败：{symbol}")
    meta = result.get("meta", {})
    prev = prev_close_from_candles(result)
    if prev is None or prev == 0:
        raise QuoteError(f"Yahoo 前收无法推导：{symbol}")
    if _in_session(result):
        price = meta.get("regularMarketPrice")
    else:
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = [c for c in (quote.get("close") or []) if c]
        price = closes[-1] if closes else None
    if price is None:
        raise QuoteError(f"Yahoo 现价无法获取：{symbol}")
    return {"symbol": symbol, "price": price, "prev_close": prev,
            "pct": (price - prev) / prev * 100.0, "source": "yahoo"}


def _tencent_code(symbol):
    if symbol.endswith(".SS"):
        return "sh" + symbol[:-3]
    if symbol.endswith(".SZ"):
        return "sz" + symbol[:-3]
    return None


def fetch_tencent(symbol):
    """腾讯行情（仅 A 股）：返回与 fetch_yahoo 同结构。字段：现价 [3]，昨收 [4]。"""
    code = _tencent_code(symbol)
    if code is None:
        raise QuoteError(f"腾讯不支持该市场：{symbol}")
    req = urllib.request.Request(f"http://qt.gtimg.cn/q={code}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        text = raw.decode("gbk", errors="ignore")
        fields = text.split("=", 1)[1].strip().strip('";').split("~")
        price = float(fields[3])
        prev = float(fields[4])
    except Exception as e:  # noqa: BLE001
        raise QuoteError(f"腾讯拉取/解析失败：{symbol} ({e})")
    if prev == 0:
        raise QuoteError(f"腾讯昨收为 0：{symbol}")
    return {"symbol": symbol, "price": price, "prev_close": prev,
            "pct": (price - prev) / prev * 100.0, "source": "tencent"}


def fetch(symbol):
    """主备链：按优先级试源，首个成功即返回；全部失败 raise（不返回空）。"""
    errors = []
    for fetcher in (fetch_yahoo, fetch_tencent):
        try:
            return fetcher(symbol)
        except QuoteError as e:
            errors.append(str(e))
    raise QuoteError(f"全部行情源失败：{symbol} :: {' | '.join(errors)}")


def cross_validate(symbol, tolerance_pp=0.5):
    """双源交叉校验：返回 (yahoo, tencent)；差值超阈值 raise DisagreementError。"""
    yahoo = fetch_yahoo(symbol)
    try:
        tencent = fetch_tencent(symbol)
    except QuoteError:
        return yahoo, None  # 备源不可用时不阻塞主源，但调用方应记录备源缺失
    if abs(yahoo["pct"] - tencent["pct"]) > tolerance_pp:
        raise DisagreementError(
            f"{symbol} 双源不一致：Yahoo {yahoo['pct']:.2f}% vs "
            f"腾讯 {tencent['pct']:.2f}%")
    return yahoo, tencent


def bp_change(prev_close, price):
    """收益率类标的（如 ^TNX）的绝对变化，单位基点（1 个收益率百分点 = 100bp）。"""
    if prev_close is None or price is None:
        return None
    return (price - prev_close) * 100.0
