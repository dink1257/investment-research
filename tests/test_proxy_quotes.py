"""代理行情测试（全部 mock，无网络，直接跑）。

覆盖：原标的无数据时改拉代理（CN=F -> 510300.SS，USDCNH=X -> CNY=X），
代理是 A 股时同样双源校验；原标的有数据时不走代理；无代理配置时仍失败。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import quotes  # noqa: E402


def _q(symbol, pct, price=100.0, prev=100.0, source="yahoo"):
    return {"symbol": symbol, "price": price, "prev_close": prev,
            "pct": pct, "source": source}


class _Stub:
    """按标的返回预设结果或抛 QuoteError。"""

    def __init__(self, mapping):
        self.mapping = mapping

    def __call__(self, symbol):
        result = self.mapping.get(symbol)
        if isinstance(result, Exception):
            raise result
        return result


def main():
    orig_yahoo = quotes.fetch_yahoo
    orig_tencent = quotes.fetch_tencent
    fail = quotes.QuoteError("no data")

    def run(yahoo_map, tencent_map, symbol, proxies):
        quotes.fetch_yahoo = _Stub(yahoo_map)
        quotes.fetch_tencent = _Stub(tencent_map)
        try:
            return quotes.fetch_validated(symbol, proxies)
        finally:
            quotes.fetch_yahoo = orig_yahoo
            quotes.fetch_tencent = orig_tencent

    proxies = {"CN=F": "510300.SS", "USDCNH=X": "CNY=X"}

    # 1. CN=F：原标的 Yahoo/腾讯都失败，代理 510300 双源一致
    q, dis, missing = run(
        {"CN=F": fail, "510300.SS": _q("510300.SS", 2.0, 4.10, 4.02)},
        {"CN=F": fail,
         "510300.SS": _q("510300.SS", 2.05, 4.10, 4.02, "tencent")},
        "CN=F", proxies)
    assert q is not None and q["symbol"] == "CN=F", q
    assert q["proxy"] == "510300.SS" and q["pct"] == 2.0, q
    assert dis is False and missing is False, (dis, missing)

    # 2. CN=F：代理双源不一致 -> disagreement，但仍返回行情
    q, dis, missing = run(
        {"CN=F": fail, "510300.SS": _q("510300.SS", 2.0, 4.10, 4.02)},
        {"CN=F": fail,
         "510300.SS": _q("510300.SS", 3.0, 4.14, 4.02, "tencent")},
        "CN=F", proxies)
    assert q is not None and q["proxy"] == "510300.SS", q
    assert dis is True and missing is False, (dis, missing)

    # 3. CN=F：代理 Yahoo 也失败 -> 全部失败
    q, dis, missing = run(
        {"CN=F": fail, "510300.SS": fail},
        {"CN=F": fail, "510300.SS": fail}, "CN=F", proxies)
    assert q is None and dis is False and missing is False, (q, dis, missing)

    # 4. CN=F：代理腾讯不可用 -> 主源行情 + backup_missing
    q, dis, missing = run(
        {"CN=F": fail, "510300.SS": _q("510300.SS", 2.0, 4.10, 4.02)},
        {"CN=F": fail, "510300.SS": fail}, "CN=F", proxies)
    assert q is not None and q["proxy"] == "510300.SS", q
    assert dis is False and missing is True, (dis, missing)

    # 5. USDCNH=X：代理 CNY=X（非 A 股，Yahoo 单源）
    q, dis, missing = run(
        {"USDCNH=X": fail, "CNY=X": _q("CNY=X", 0.12, 6.66, 6.65)},
        {"USDCNH=X": fail, "CNY=X": fail}, "USDCNH=X", proxies)
    assert q is not None and q["symbol"] == "USDCNH=X", q
    assert q["proxy"] == "CNY=X" and q["pct"] == 0.12, q
    assert dis is False and missing is False, (dis, missing)

    # 6. USDCNH=X：代理也失败 -> 全部失败
    q, dis, missing = run(
        {"USDCNH=X": fail, "CNY=X": fail},
        {"USDCNH=X": fail, "CNY=X": fail}, "USDCNH=X", proxies)
    assert q is None, q

    # 7. USDCNH=X：没配代理 -> 全部失败（不静默编造）
    q, dis, missing = run(
        {"USDCNH=X": fail}, {"USDCNH=X": fail}, "USDCNH=X", {})
    assert q is None, q

    # 8. 原标的有数据时不走代理（CN=F 万一恢复上架）
    q, dis, missing = run(
        {"CN=F": _q("CN=F", 1.5, 13000.0, 12800.0)},
        {"CN=F": fail}, "CN=F", proxies)
    assert q is not None and "proxy" not in q and q["pct"] == 1.5, q
    assert dis is False and missing is False, (dis, missing)

    # 9. 普通 A 股行为不变：双源一致，无 proxy 字段
    q, dis, missing = run(
        {"600519.SS": _q("600519.SS", 0.5, 1700.0, 1690.0)},
        {"600519.SS": _q("600519.SS", 0.52, 1700.0, 1690.0, "tencent")},
        "600519.SS", proxies)
    assert q is not None and "proxy" not in q and q["pct"] == 0.5, q
    assert dis is False and missing is False, (dis, missing)

    # 10. 普通非 A 股行为不变：主备链，无 proxy 字段
    q, dis, missing = run(
        {"AAPL": _q("AAPL", -1.0, 330.0, 333.0)},
        {"AAPL": fail}, "AAPL", proxies)
    assert q is not None and "proxy" not in q and q["pct"] == -1.0, q

    print("test_proxy_quotes: all passed")


if __name__ == "__main__":
    main()
