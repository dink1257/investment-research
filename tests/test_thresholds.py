"""阈值引擎单元测试（无第三方依赖，直接跑）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.thresholds import classify, pct_change


def close(a, b, eps=1e-9):
    assert abs(a - b) < eps, f"{a} != {b}"


def main():
    close(pct_change(100, 103.5), 3.5)
    close(pct_change(100, 96.0), -4.0)
    assert pct_change(0, 100) is None
    assert pct_change(None, 100) is None

    maotai = {"unit": "pct", "regular": 3.0, "emergency": 3.859}
    assert classify(maotai, pct=3.0) == "regular"      # 边界含等于
    assert classify(maotai, pct=3.859) == "emergency"  # 边界含等于
    assert classify(maotai, pct=2.99) == "none"
    assert classify(maotai, pct=-4.0) == "emergency"   # 跌幅取绝对值
    assert classify(maotai, pct=None) == "none"

    tnx = {"unit": "bp", "regular_bp": 10, "emergency_bp": 20}
    assert classify(tnx, bp=10) == "regular"
    assert classify(tnx, bp=20) == "emergency"
    assert classify(tnx, bp=9.9) == "none"
    assert classify(tnx, bp=-25) == "emergency"
    print("阈值引擎测试全部通过")


if __name__ == "__main__":
    main()
