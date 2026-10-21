"""Authenticated WeChat AES256-CBC callback transport; no verdict interpretation."""

import base64
import hashlib
import hmac
import re
import struct
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.settings import Settings
from app.wechat import security_unavailable, strict_json

_MAX_BODY = 64 * 1024
_UNSAFE_XML = re.compile(r"<\s*(?:\?|!(?!\[CDATA\[|--))", re.IGNORECASE)


def _xml_value(element: ET.Element, depth: int = 0):
    if depth > 32 or element.attrib:
        raise ValueError("Invalid XML structure")
    if not len(element):
        return element.text or ""
    if element.text and element.text.strip():
        raise ValueError("Mixed XML content")
    result = {}
    for child in element:
        if child.tail and child.tail.strip():
            raise ValueError("Ambiguous XML structure")
        value = _xml_value(child, depth + 1)
        if child.tag in result:
            if not isinstance(result[child.tag], list):
                result[child.tag] = [result[child.tag]]
            result[child.tag].append(value)
        else:
            result[child.tag] = value
    return result


def _parse(raw: bytes) -> dict:
    if not raw or len(raw) > _MAX_BODY:
        raise ValueError("Invalid body length")
    text = raw.decode("utf-8").strip()
    if text.startswith("{"):
        return strict_json(raw)
    if not text.startswith("<") or _UNSAFE_XML.search(text):
        raise ValueError("Invalid XML")
    root = ET.fromstring(text)
    if root.tag != "xml" or sum(1 for _ in root.iter()) > 2048:
        raise ValueError("Invalid XML root")
    result = _xml_value(root)
    if not isinstance(result, dict):
        raise ValueError("Expected object")
    return result


class WeChatCallback:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _verify(self, params: Mapping[str, str], encrypted: str | None) -> None:
        if not self.settings.security_configured:
            raise ValueError("Missing callback configuration")
        timestamp, nonce = params.get("timestamp", ""), params.get("nonce", "")
        if (
            not isinstance(timestamp, str)
            or not re.fullmatch(r"[0-9]{1,12}", timestamp)
            or abs(time.time() - int(timestamp)) > 600
            or not isinstance(nonce, str)
            or not 0 < len(nonce) <= 256
        ):
            raise ValueError("Invalid timestamp or nonce")
        fields = [
            self.settings.wechat_callback_token.get_secret_value(),
            timestamp,
            nonce,
        ]
        name = "signature"
        if encrypted is not None:
            fields.append(encrypted)
            name = "msg_signature"
        signature = params.get(name, "")
        if not isinstance(signature, str) or not re.fullmatch(
            r"[0-9a-f]{40}", signature
        ):
            raise ValueError("Invalid signature")
        expected = hashlib.sha1("".join(sorted(fields)).encode("utf-8")).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid signature")

    def _decrypt(self, encrypted: str) -> bytes:
        if not isinstance(encrypted, str) or not 0 < len(encrypted) <= _MAX_BODY:
            raise ValueError("Invalid ciphertext")
        key = base64.b64decode(
            self.settings.wechat_encoding_aes_key.get_secret_value() + "=",
            validate=True,
        )
        ciphertext = base64.b64decode(encrypted, validate=True)
        if not ciphertext or len(ciphertext) % 32:
            raise ValueError("Invalid cipher length")
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        padding = padded[-1]
        if not 1 <= padding <= 32 or not hmac.compare_digest(
            padded[-padding:], bytes([padding]) * padding
        ):
            raise ValueError("Invalid padding")
        raw = padded[:-padding]
        if len(raw) < 20:
            raise ValueError("Invalid envelope")
        size = struct.unpack("!I", raw[16:20])[0]
        end = 20 + size
        expected_appid = self.settings.wechat_app_id.encode("utf-8")
        if (
            size == 0
            or end > len(raw)
            or not hmac.compare_digest(raw[end:], expected_appid)
        ):
            raise ValueError("Invalid AppID or payload length")
        return raw[20:end]

    def verify_url(self, params: Mapping[str, str]) -> str:
        try:
            if params.get("encrypt_type") not in (None, "aes"):
                raise ValueError("Invalid encryption mode")
            echo = params.get("echostr", "")
            if (
                not isinstance(echo, str)
                or not 0 < len(echo.encode("utf-8")) <= _MAX_BODY
            ):
                raise ValueError("Invalid challenge")
            encrypted = params.get("encrypt_type") == "aes" or "msg_signature" in params
            self._verify(params, echo if encrypted else None)
            return self._decrypt(echo).decode("utf-8") if encrypted else echo
        except (ValueError, TypeError, UnicodeError):
            raise security_unavailable() from None

    def decode_event(self, params: Mapping[str, str], body: bytes) -> dict:
        try:
            if params.get("encrypt_type") != "aes":
                raise ValueError("Encrypted mode required")
            outer = _parse(body)
            encrypted = outer.get("Encrypt")
            if not isinstance(encrypted, str):
                raise ValueError("Missing ciphertext")
            for name, value in outer.items():
                if name.lower() == "appid" and value != self.settings.wechat_app_id:
                    raise ValueError("Invalid outer AppID")
            self._verify(params, encrypted)
            return _parse(self._decrypt(encrypted))
        except (ValueError, TypeError, UnicodeError, ET.ParseError, RecursionError):
            raise security_unavailable() from None
