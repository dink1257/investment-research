"""A 隐私重做（E' 的修订）：Fernet 加密 payload、--quiet、
run_id/attempt、哨兵状态路径。LLM 全部 mock，无网络；
密钥测试时生成，绝不写进文件。"""
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

from cryptography.fernet import Fernet  # noqa: E402
from agents import debate as debate_mod  # noqa: E402
from agents import deepdive as deepdive_mod  # noqa: E402
from services import sentinel as sentinel_mod  # noqa: E402
from tools import input_crypto as input_crypto_mod  # noqa: E402
from tools import last_analysis as la_mod  # noqa: E402
import run_debate as run_debate_mod  # noqa: E402
import run_deepdive as run_deepdive_mod  # noqa: E402

TS = "2026-11-10T22:30:00+08:00"
BRIEF = "Q3 财报后股价异动，想知道基本面有没有变坏"
TOPIC = "人形机器人板块近期大涨能否持续"


def _write_config(path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("budget:\n  monthly_cap: 20\n  warn_at: 16\n"
                "  daily_debate_cap: 8\n  daily_deepdive_cap: 2\n"
                "  dedup_hours: 2\nthresholds:\n  symbols: {}\n")


def _set_key(key):
    """换测试密钥，返回旧值（测试后恢复）。key 为 None 表示删掉。"""
    old = os.environ.get("HERMES_INPUT_KEY")
    if key is None:
        os.environ.pop("HERMES_INPUT_KEY", None)
    else:
        os.environ["HERMES_INPUT_KEY"] = key
    return old


def _restore_key(old):
    if old is None:
        os.environ.pop("HERMES_INPUT_KEY", None)
    else:
        os.environ["HERMES_INPUT_KEY"] = old


def _run_cli(mod, argv):
    """跑 CLI main()，返回 (exit_code, 捕获的 stdout)。"""
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = argv
        with contextlib.redirect_stdout(buf):
            return mod.main(), buf.getvalue()
    finally:
        sys.argv = old_argv


def _read_payload(state):
    with open(os.path.join(state, "last_analysis.json"),
              encoding="utf-8") as f:
        return json.load(f)


def _enc_payload(key, payload):
    """把一个 dict 加密成 Fernet 密文（Hermes 派发的 payload 形态）。"""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Fernet(key.encode("utf-8")).encrypt(raw).decode("utf-8")


def test_fernet_roundtrip_unit():
    key = Fernet.generate_key().decode("utf-8")
    old = _set_key(key)
    try:
        cipher = Fernet(key.encode("utf-8")).encrypt(BRIEF.encode("utf-8"))
        assert input_crypto_mod.decrypt(cipher.decode("utf-8")) == BRIEF
    finally:
        _restore_key(old)


def test_fernet_missing_key():
    old = _set_key(None)
    try:
        try:
            input_crypto_mod.decrypt("anything")
        except input_crypto_mod.InputCryptoError as exc:
            assert str(exc) == "decrypt_key_missing"
        else:
            raise AssertionError("缺密钥时应抛错")
    finally:
        _restore_key(old)


def test_fernet_wrong_key_and_garbage():
    good = Fernet.generate_key().decode("utf-8")
    cipher = Fernet(good.encode("utf-8")).encrypt(TOPIC.encode("utf-8"))
    old = _set_key(Fernet.generate_key().decode("utf-8"))
    try:
        try:
            input_crypto_mod.decrypt(cipher.decode("utf-8"))
        except input_crypto_mod.InputCryptoError as exc:
            assert str(exc) == "decrypt_failed"
        else:
            raise AssertionError("错密钥时应抛错")
        try:
            input_crypto_mod.decrypt("not-a-ciphertext")
        except input_crypto_mod.InputCryptoError as exc:
            assert str(exc) == "decrypt_failed"
        else:
            raise AssertionError("坏密文时应抛错")
    finally:
        _restore_key(old)


def test_decrypt_payload_unit():
    """decrypt_payload：JSON 对象往返；非对象/非 JSON 按解密失败处理。"""
    key = Fernet.generate_key().decode("utf-8")
    old = _set_key(key)
    try:
        cipher = _enc_payload(key, {"symbol": "300750.SZ", "brief": BRIEF})
        assert input_crypto_mod.decrypt_payload(cipher) == {
            "symbol": "300750.SZ", "brief": BRIEF}
        bad = Fernet(key.encode("utf-8")).encrypt(b"[1, 2]")
        try:
            input_crypto_mod.decrypt_payload(bad.decode("utf-8"))
        except input_crypto_mod.InputCryptoError as exc:
            assert str(exc) == "decrypt_failed"
        else:
            raise AssertionError("非对象 JSON 应抛错")
        plain = Fernet(key.encode("utf-8")).encrypt(b"not json")
        try:
            input_crypto_mod.decrypt_payload(plain.decode("utf-8"))
        except input_crypto_mod.InputCryptoError as exc:
            assert str(exc) == "decrypt_failed"
        else:
            raise AssertionError("非 JSON 明文应抛错")
    finally:
        _restore_key(old)


def _deepdive_report():
    return {"verdict": "观望：数据不足", "buy_point": None,
            "sell_point": None, "risk_strategy": "等数据",
            "fundamental": "f", "technical": "t", "capital": "c",
            "risks": "r", "uncertain": ["u"],
            "backtest": {"skipped": "buy_point 无数值"}}


def test_deepdive_cli_encrypted_payload_quiet():
    """密文 payload 经 CLI 解密：symbol 和 brief 都从 payload 来，
    不传明文 --symbol；--quiet 时报告与明文都不进 stdout；
    run_id/attempt 写进结果文件。"""
    key = Fernet.generate_key().decode("utf-8")
    cipher = _enc_payload(key, {"symbol": "300750.SZ", "brief": BRIEF})
    old_key = _set_key(key)
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        captured = {}
        old = deepdive_mod.run_deepdive

        def fake(*a, **k):
            captured["symbol"] = a[2]
            captured["brief"] = a[4]
            return {"report": _deepdive_report(), "cost_rmb": 0.0062,
                    "blocked": None}

        deepdive_mod.run_deepdive = fake
        try:
            code, out = _run_cli(
                run_deepdive_mod,
                ["run_deepdive.py", "--config", cfg, "--db", db,
                 "--payload-encrypted", cipher,
                 "--task-id", "T-E1",
                 "--budget-cny", "0.5",
                 "--deadline-utc", "2099-01-01T00:00:00Z",
                 "--state-dir", state, "--quiet",
                 "--run-id", "123456", "--attempt", "2"])
        finally:
            deepdive_mod.run_deepdive = old
            _restore_key(old_key)
        assert code == 0, out
        assert captured["symbol"] == "300750.SZ"
        assert captured["brief"] == BRIEF
        assert BRIEF not in out  # 明文绝不进 stdout
        assert "300750.SZ" not in out  # 标的代码也不进 stdout
        assert "观望：数据不足" not in out  # --quiet：报告不打印
        payload = _read_payload(state)
        assert payload["status"] == "success"
        assert payload["symbol"] == "300750.SZ"
        assert payload["task_id"] == "T-E1"
        assert payload["run_id"] == 123456
        assert payload["attempt"] == 2
        with open(os.path.join(state, "last_analysis.md"),
                  encoding="utf-8") as f:
            assert "观望：数据不足" in f.read()  # 报告仍写 state 文件


def test_deepdive_cli_loud_prints_report():
    """不带 --quiet 时报告照常打印（对照：--quiet 是修日志泄漏的开关）。"""
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        old = deepdive_mod.run_deepdive
        deepdive_mod.run_deepdive = lambda *a, **k: {
            "report": _deepdive_report(), "cost_rmb": 0.0062,
            "blocked": None}
        try:
            code, out = _run_cli(
                run_deepdive_mod,
                ["run_deepdive.py", "--config", cfg, "--db", db,
                 "--symbol", "300750.SZ", "--brief", "本地手动问题",
                 "--task-id", "T-E2", "--state-dir", state])
        finally:
            deepdive_mod.run_deepdive = old
        assert code == 0, out
        assert "观望：数据不足" in out


def test_deepdive_cli_decrypt_failure():
    """解密失败写 failed 文件、不调 LLM、不花钱、明文不进 stdout。"""
    good = Fernet.generate_key().decode("utf-8")
    cipher = _enc_payload(good, {"symbol": "300750.SZ", "brief": BRIEF})
    old_key = _set_key(Fernet.generate_key().decode("utf-8"))  # 错密钥
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        called = []
        old = deepdive_mod.run_deepdive
        deepdive_mod.run_deepdive = lambda *a, **k: called.append(True) or {
            "report": {}, "cost_rmb": 0.0, "blocked": None}
        try:
            code, out = _run_cli(
                run_deepdive_mod,
                ["run_deepdive.py", "--config", cfg, "--db", db,
                 "--payload-encrypted", cipher,
                 "--task-id", "T-E3",
                 "--state-dir", state, "--quiet"])
        finally:
            deepdive_mod.run_deepdive = old
            _restore_key(old_key)
        assert code == 1, out
        assert called == []  # 未调 LLM
        assert BRIEF not in out
        payload = _read_payload(state)
        assert payload["status"] == "failed"
        assert payload["error"] == "decrypt_failed"
        assert payload["cost_cny"] == 0.0
        assert payload["task_id"] == "T-E3"


def test_deepdive_cli_decrypt_missing_key():
    """环境里没密钥时直接拒绝解密，不调 LLM。"""
    key = Fernet.generate_key().decode("utf-8")
    cipher = _enc_payload(key, {"symbol": "300750.SZ", "brief": BRIEF})
    old_key = _set_key(None)
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        called = []
        old = deepdive_mod.run_deepdive
        deepdive_mod.run_deepdive = lambda *a, **k: called.append(True) or {
            "report": {}, "cost_rmb": 0.0, "blocked": None}
        try:
            code, out = _run_cli(
                run_deepdive_mod,
                ["run_deepdive.py", "--config", cfg, "--db", db,
                 "--payload-encrypted", cipher,
                 "--task-id", "T-E4",
                 "--state-dir", state, "--quiet"])
        finally:
            deepdive_mod.run_deepdive = old
            _restore_key(old_key)
        assert code == 1, out
        assert called == []
        payload = _read_payload(state)
        assert payload["status"] == "failed"
        assert payload["error"] == "decrypt_key_missing"


def test_deepdive_cli_payload_missing_symbol():
    """payload 里缺 symbol 按解密失败处理，不调 LLM。"""
    key = Fernet.generate_key().decode("utf-8")
    cipher = _enc_payload(key, {"brief": BRIEF})
    old_key = _set_key(key)
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        called = []
        old = deepdive_mod.run_deepdive
        deepdive_mod.run_deepdive = lambda *a, **k: called.append(True) or {
            "report": {}, "cost_rmb": 0.0, "blocked": None}
        try:
            code, out = _run_cli(
                run_deepdive_mod,
                ["run_deepdive.py", "--config", cfg, "--db", db,
                 "--payload-encrypted", cipher,
                 "--task-id", "T-E8",
                 "--state-dir", state, "--quiet"])
        finally:
            deepdive_mod.run_deepdive = old
            _restore_key(old_key)
        assert code == 1, out
        assert called == []
        payload = _read_payload(state)
        assert payload["status"] == "failed"
        assert payload["error"] == "decrypt_failed"


def test_debate_cli_encrypted_payload_quiet():
    """密文 payload 经 CLI 解密进辩论；--quiet 时结论不进 stdout。"""
    key = Fernet.generate_key().decode("utf-8")
    cipher = _enc_payload(key, {"topic": TOPIC})
    old_key = _set_key(key)
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        captured = {}
        verdict = {"verdict": "看多：短期催化明确", "confidence": 3,
                   "target_range": "70-80", "position": "轻仓",
                   "stop_loss": "跌破 60", "bull": "b", "bear": "c",
                   "bull_rebuttal": "br", "bear_rebuttal": "cr",
                   "uncertain": ["u1"]}
        old = debate_mod.run_topic_debate

        def fake(*a, **k):
            captured["topic"] = a[2]
            return {"verdict": verdict, "cost_rmb": 0.0091,
                    "blocked": None}

        debate_mod.run_topic_debate = fake
        try:
            code, out = _run_cli(
                run_debate_mod,
                ["run_debate.py", "--config", cfg, "--db", db,
                 "--payload-encrypted", cipher,
                 "--task-id", "T-E5", "--budget-cny", "0.5",
                 "--deadline-utc", "2099-01-01T00:00:00Z",
                 "--state-dir", state, "--quiet",
                 "--run-id", "123457", "--attempt", "1"])
        finally:
            debate_mod.run_topic_debate = old
            _restore_key(old_key)
        assert code == 0, out
        assert captured["topic"] == TOPIC
        assert TOPIC not in out
        assert "看多：短期催化明确" not in out
        payload = _read_payload(state)
        assert payload["status"] == "success"
        assert payload["task_id"] == "T-E5"
        assert payload["symbol"] == "topic:" + TOPIC[:60]
        assert payload["topic"] == TOPIC
        assert payload["run_id"] == 123457
        assert payload["attempt"] == 1
        with open(os.path.join(state, "last_analysis.md"),
                  encoding="utf-8") as f:
            assert "看多：短期催化明确" in f.read()


def test_debate_cli_requires_topic():
    """--topic 与 --payload-encrypted 都没给时报错退出（不调 LLM）。"""
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data", "ledger.db")
        state = os.path.join(d, "state")
        called = []
        old = debate_mod.run_topic_debate
        debate_mod.run_topic_debate = lambda *a, **k: called.append(True)
        try:
            code, out = _run_cli(
                run_debate_mod,
                ["run_debate.py", "--config", cfg, "--db", db,
                 "--task-id", "T-E6", "--state-dir", state])
        except SystemExit as exc:
            code = exc.code
        finally:
            debate_mod.run_topic_debate = old
        assert code == 2
        assert called == []


def test_payload_run_id_defaults_none():
    report = {"verdict": "观望", "uncertain": []}
    p = la_mod.deepdive_payload("300750.SZ", "宁德时代", report, 0.01, TS)
    assert p["run_id"] is None and p["attempt"] is None
    p2 = la_mod.deepdive_payload("300750.SZ", "宁德时代", report, 0.01, TS,
                                run_id=777, attempt=3)
    assert p2["run_id"] == 777 and p2["attempt"] == 3
    r = la_mod.running_payload("T-E7", "debate", topic=TOPIC, ts=TS,
                               run_id=778, attempt=1)
    assert r["run_id"] == 778 and r["attempt"] == 1
    assert r["status"] == "running"


def test_sentinel_status_path_follows_db():
    """哨兵源状态文件写在 --db 所在目录，不再写死仓库根 data/。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.yaml")
        _write_config(cfg)
        db = os.path.join(d, "data-store", "data", "ledger.db")
        old_argv = sys.argv
        buf = io.StringIO()
        try:
            sys.argv = ["sentinel.py", "--config", cfg, "--db", db,
                        "--dry-run"]
            with contextlib.redirect_stdout(buf):
                assert sentinel_mod.main() == 0
        finally:
            sys.argv = old_argv
        expect = os.path.join(d, "data-store", "data",
                              "sentinel_sources.json")
        assert os.path.exists(expect), expect
        assert os.path.exists(db)
        assert not os.path.exists(os.path.join(
            root, "data", "sentinel_sources.json"))


def main():
    test_fernet_roundtrip_unit()
    test_fernet_missing_key()
    test_fernet_wrong_key_and_garbage()
    test_decrypt_payload_unit()
    test_deepdive_cli_encrypted_payload_quiet()
    test_deepdive_cli_loud_prints_report()
    test_deepdive_cli_decrypt_failure()
    test_deepdive_cli_decrypt_missing_key()
    test_deepdive_cli_payload_missing_symbol()
    test_debate_cli_encrypted_payload_quiet()
    test_debate_cli_requires_topic()
    test_payload_run_id_defaults_none()
    test_sentinel_status_path_follows_db()
    print("test_privacy_eprime: all passed")


if __name__ == "__main__":
    main()
