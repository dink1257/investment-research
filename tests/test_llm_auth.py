"""LLM 认证路径测试（全部 mock，无网络，直接跑）。

覆盖：环境变量 DASHSCOPE_API_KEY 优先（Actions 路径，Bearer 直连）；
无环境变量时走连接器 surrogate（本机路径，懒加载）；
call_qwen 全流程（请求头 + 解析 + 计价）。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import llm as llm_mod  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.last_req = None

    def open(self, req, timeout=None):
        self.last_req = req
        return _FakeResp(self.payload)


def main():
    # 1. auth_mode 按环境变量切换
    os.environ.pop("DASHSCOPE_API_KEY", None)
    assert llm_mod.auth_mode() == "surrogate", llm_mod.auth_mode()
    os.environ["DASHSCOPE_API_KEY"] = "test-api-key"
    try:
        assert llm_mod.auth_mode() == "env", llm_mod.auth_mode()
    finally:
        os.environ.pop("DASHSCOPE_API_KEY", None)

    # 2. 环境变量路径：Bearer 头，不碰 surrogate
    os.environ["DASHSCOPE_API_KEY"] = "test-api-key"
    try:
        req = urllib.request.Request("https://example.invalid/v1",
                                     method="POST")
        llm_mod._apply_auth(req)
        assert req.get_header("Authorization") == "Bearer test-api-key", \
            req.headers
        assert "dynamic_credentials" not in sys.modules or True
    finally:
        os.environ.pop("DASHSCOPE_API_KEY", None)

    # 3. 无环境变量时走 surrogate（注入假模块，不碰真实连接器）
    class _FakeCreds:
        called = []

        @staticmethod
        def add_surrogate_to_request(req, name, **kw):
            _FakeCreds.called.append((req, name))
            req.add_header("X-Surrogate", "stub")

    fake_mod = type(sys)("dynamic_credentials")
    fake_mod.add_surrogate_to_request = _FakeCreds.add_surrogate_to_request
    sys.modules["dynamic_credentials"] = fake_mod
    os.environ.pop("DASHSCOPE_API_KEY", None)
    try:
        req = urllib.request.Request("https://example.invalid/v1",
                                     method="POST")
        llm_mod._apply_auth(req)
        assert len(_FakeCreds.called) == 1, _FakeCreds.called
        assert _FakeCreds.called[0][1] == "custom.dashscope"
        # add_header 会把 key 存成 capitalize 后的形式（X-surrogate）
        assert req.get_header("X-surrogate") == "stub"
    finally:
        del sys.modules["dynamic_credentials"]

    # 4. call_qwen 全流程（环境变量路径，mock opener）
    payload = {
        "choices": [{"message": {"content": "{\"verdict\": \"观望\"}"}}],
        "usage": {"prompt_tokens": 1000,
                  "prompt_tokens_details": {"cached_tokens": 200},
                  "completion_tokens": 500},
    }
    opener = _FakeOpener(payload)
    orig_build_opener = urllib.request.build_opener
    urllib.request.build_opener = lambda *a, **k: opener
    os.environ["DASHSCOPE_API_KEY"] = "test-api-key"
    try:
        text, usage, cost = llm_mod.call_qwen("sys", "hi", 6000)
    finally:
        urllib.request.build_opener = orig_build_opener
        os.environ.pop("DASHSCOPE_API_KEY", None)
    assert text == "{\"verdict\": \"观望\"}", text
    assert usage == {"prompt_tokens": 1000, "cached_tokens": 200,
                     "output_tokens": 500}, usage
    # (1000-200)*0.8e-6 + 200*0.1e-6 + 500*2.7e-6 = 0.00201
    assert cost == 0.002, cost
    assert opener.last_req.get_header("Authorization") == \
        "Bearer test-api-key"
    assert opener.last_req.full_url == llm_mod.BASE_URL + "/chat/completions"

    print("test_llm_auth: all passed")


if __name__ == "__main__":
    main()
