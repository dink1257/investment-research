"""A 输入解密：Fernet 对称解密（Hermes 派发的加密 payload）。

payload 是 JSON 对象：深研 {"symbol": ..., "brief": ...}，
辩论 {"topic": ...}。密钥只从环境变量 HERMES_INPUT_KEY 读，
绝不写进代码、不打印、不记日志。解密只在内存里做；失败时抛
InputCryptoError，调用方写 failed 结果文件、不调 LLM、不花钱。
空字符串不解密（调用方先判空）。
"""
import json
import os


class InputCryptoError(ValueError):
    """解密失败：缺密钥 / 密钥无效 / 密文损坏 / 缺 cryptography 依赖。"""


def decrypt(ciphertext):
    """解密一段 Fernet 密文（base64 字符串）-> 明文字符串。"""
    key = os.environ.get("HERMES_INPUT_KEY")
    if not key:
        raise InputCryptoError("decrypt_key_missing")
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        raise InputCryptoError("decrypt_unavailable")
    try:
        fernet = Fernet(key.encode("utf-8"))
    except ValueError:
        raise InputCryptoError("decrypt_failed")
    try:
        return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise InputCryptoError("decrypt_failed")


def decrypt_payload(ciphertext):
    """解密一段 Fernet 密文（base64 字符串）-> 解析后的 JSON dict。

    明文必须是 JSON 对象；不是对象、解析失败，都按解密失败处理。
    """
    text = decrypt(ciphertext)
    try:
        payload = json.loads(text)
    except ValueError:
        raise InputCryptoError("decrypt_failed")
    if not isinstance(payload, dict):
        raise InputCryptoError("decrypt_failed")
    return payload
