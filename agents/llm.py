"""LLM 调用层：Qwen3.8-Flash（DashScope 兼容端点），复用 A/B 验证过的范式。

- 凭据不进代码，有两条路径（2026-11-09 补齐 Actions 路径）：
  1. 环境变量 DASHSCOPE_API_KEY（GitHub Actions：workflow 从仓库 secrets
     注入，Bearer 直连）；
  2. 无环境变量时走 Secure Vault surrogate（custom.dashscope，本机同款）。
  surrogate 的 import 是懒加载——Actions 上没有这条路径，模块顶层绝不能
  import 它，否则整个 agents 包在 Actions 里无法加载。
- 计价：百炼北京价，输入 ¥0.8/M、缓存命中 ¥0.1/M、输出 ¥2.7/M（A/B 实测公式）。
- JSON 围栏剥离：模型常在 JSON 外包一层 ```json 围栏，先剥再解析。
- enable_thinking=True；思考 token 计入 max_tokens（A/B 教训：上限要留足，
  辩论 6000（2026-10-30 新风格后由 4000 上调）、深研 8000 是实测过的够用值）。
"""
import json
import os
import sys
import urllib.request

BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL = "qwen3.8-flash"
PRICE = {"in_miss": 0.8 / 1e6, "in_hit": 0.1 / 1e6, "out": 2.7 / 1e6}
SYSTEM_PROMPT = "你是投资研判系统的分析模块，只输出 JSON。"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def auth_mode():
    """当前生效的认证路径：'env'（环境变量）或 'surrogate'（连接器）。"""
    return "env" if os.environ.get("DASHSCOPE_API_KEY") else "surrogate"


def _read_json(resp):
    """读 JSON 响应体（与 dynamic_credentials.read_json_response 同款，
    本地实现以便无连接器时也能用）。"""
    return json.loads(resp.read().decode("utf-8"))


def _apply_auth(req):
    """给请求加认证。环境变量优先（Actions），否则走连接器 surrogate（本机）。
    surrogate 的 import 在函数内懒加载，顶层 import 会让模块在 Actions
    上加载失败。"""
    key = os.environ.get("DASHSCOPE_API_KEY")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
        return
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import add_surrogate_to_request
    add_surrogate_to_request(req, "custom.dashscope",
                             allowed_hosts=["dashscope.aliyuncs.com"])


def call_qwen(system_prompt, user_text, max_tokens, temperature=0.2):
    """调一次 Qwen。返回 (text, usage, cost_rmb)。
    usage: {prompt_tokens, cached_tokens, output_tokens}。"""
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "enable_thinking": True,
    }
    req = urllib.request.Request(
        BASE_URL + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    _apply_auth(req)
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(req, timeout=180) as resp:
        data = _read_json(resp)
    usage_raw = data.get("usage", {}) or {}
    detail = usage_raw.get("prompt_tokens_details", {}) or {}
    usage = {
        "prompt_tokens": usage_raw.get("prompt_tokens", 0),
        "cached_tokens": detail.get("cached_tokens", 0),
        "output_tokens": usage_raw.get("completion_tokens", 0),
    }
    cost = ((usage["prompt_tokens"] - usage["cached_tokens"])
            * PRICE["in_miss"]
            + usage["cached_tokens"] * PRICE["in_hit"]
            + usage["output_tokens"] * PRICE["out"])
    text = (data["choices"][0]["message"].get("content") or "")
    return text, usage, round(cost, 4)


def strip_fence(text):
    """剥 ```json 围栏（A/B 时已验证的容错：围栏本身不算 JSON 失败）。"""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    return stripped


def parse_json(text):
    """解析模型输出的 JSON。失败 raise ValueError（调用方重试一次）。"""
    try:
        return json.loads(strip_fence(text))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"JSON 解析失败：{e}")
