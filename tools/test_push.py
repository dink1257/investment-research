"""测试推送：经 push.send_alert（紧急档）发一条测试消息，验证飞书 + Bark。

只在手动触发时跑（workflow_dispatch 的 test_push 输入），不碰行情与账本。
用法：python3 tools/test_push.py --config config.runtime.yaml
返回值里不含任何密钥，可直接打印到 Actions 日志；无通道送达时退出码为 1。
"""
import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402
from push import push as push_mod  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="测试推送（飞书 + Bark）")
    parser.add_argument("--config", required=True,
                        help="运行期配置（config.runtime.yaml）")
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    title = "投资研究推送测试"
    content = (f"这是一条测试推送，用于验证飞书与 Bark 通道。"
               f"时间：{ts}（北京时间）。请忽略。")
    result = push_mod.send_alert(config, title, content, level="emergency")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("delivered"):
        print("测试推送未送达：没有任何通道成功。")
        raise SystemExit(1)
    print("测试推送已送达（至少一个通道成功）。")


if __name__ == "__main__":
    main()
