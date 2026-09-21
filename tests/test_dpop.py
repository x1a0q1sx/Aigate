# -*- coding: utf-8 -*-
"""u1s1 DPoP proof：签发结构与密码学校验（对齐官方 CLI device-auth.js 形态）。"""
import base64
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives import hashes

from server.core.dpop import sign_proof, private_key_from_jwk, public_jwk_from


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _make_jwk():
    priv = ec.generate_private_key(ec.SECP256R1())
    pn = priv.private_numbers()
    pub = pn.public_numbers

    def coord(n):
        return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()

    return {"kty": "EC", "crv": "P-256", "x": coord(pub.x), "y": coord(pub.y), "d": coord(pn.private_value)}

def test_proof_structure_and_signature():
    jwk = _make_jwk()
    token = "u1s1d-test-token"
    h = sign_proof(jwk, token, "post", "https://api.u1s1.io/v1/chat/completions?x=1#frag", now=1700000000)
    assert h["Authorization"] == f"DPoP {token}"
    header_b64, payload_b64, sig_b64 = h["DPoP"].split(".")
    header = json.loads(_b64u_decode(header_b64))
    payload = json.loads(_b64u_decode(payload_b64))
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256"
    # header 内嵌公钥 JWK：与私钥推导一致，且不含私有字段
    assert header["jwk"] == public_jwk_from(private_key_from_jwk(jwk))
    assert set(header["jwk"]) == {"kty", "crv", "x", "y", "ext"}
    # claims：htm 大写、htu 去 query/hash、ath=sha256(b64url(token))
    assert payload["htm"] == "POST"
    assert payload["htu"] == "https://api.u1s1.io/v1/chat/completions"
    assert payload["iat"] == 1700000000
    assert payload["ath"] == base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest()).rstrip(b"=").decode()
    assert len(payload["jti"]) == 32
    # 签名：raw r||s（64 字节）→ DER 后用公钥验证
    sig = _b64u_decode(sig_b64)
    assert len(sig) == 64
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    private_key_from_jwk(jwk).public_key().verify(
        der, f"{header_b64}.{payload_b64}".encode(), ec.ECDSA(hashes.SHA256()))


def test_htt_uses_build_url_for_get():
    jwk = _make_jwk()
    h = sign_proof(jwk, "u1s1d-t", "GET", "https://api.u1s1.io/v1/models")
    _, payload_b64, _ = h["DPoP"].split(".")
    payload = json.loads(_b64u_decode(payload_b64))
    assert payload["htm"] == "GET" and payload["htu"] == "https://api.u1s1.io/v1/models"


def test_openai_compat_headers_apply_dpop_and_strip_marker():
    from server.adapters.openai_compat import OpenAICompatAdapter
    jwk = _make_jwk()
    a = OpenAICompatAdapter()
    extra = {"__oauth": True, "__dpop": {"token": "u1s1d-marker", "jwk": jwk}}
    headers = a._get_headers("u1s1d-marker", extra, "https://api.u1s1.io/v1",
                             url="https://api.u1s1.io/v1/chat/completions", method="POST")
    assert headers["Authorization"] == "DPoP u1s1d-marker"
    assert "__dpop" not in headers and "." in headers["DPoP"]
    # 客户端完整性审查判据：出站必须带 user-agent: u1s1-cli（实测缺它 → 403 client_integrity_review）
    assert headers.get("user-agent") == "u1s1-cli"
    # 无 __dpop 时保持 Bearer 旧行为
    h2 = a._get_headers("u1s1-key", {"__oauth": True}, "https://api.u1s1.io/v1",
                        url="https://api.u1s1.io/v1/chat/completions")
    assert h2["Authorization"] == "Bearer u1s1-key" and "DPoP" not in h2


def test_openai_compat_headers_apply_attestation():
    from server.adapters.openai_compat import OpenAICompatAdapter
    jwk = _make_jwk()
    a = OpenAICompatAdapter()
    extra = {"__oauth": True, "__dpop": {"token": "u1s1d-m", "jwk": jwk, "att": "ATT-TOKEN"}}
    headers = a._get_headers("u1s1d-m", extra, "https://api.u1s1.io/v1",
                             url="https://api.u1s1.io/v1/chat/completions", method="POST")
    assert headers.get("x-u1s1-attestation") == "ATT-TOKEN"
    assert headers["Authorization"] == "DPoP u1s1d-m"  # att 缺失与否不影响 DPoP
    # 冷却期 att=None：不带该头（对齐 CLI：拿不到 token 就裸发，失败由冷却兜底）
    extra2 = {"__dpop": {"token": "u1s1d-m2", "jwk": jwk, "att": None}}
    h2 = a._get_headers("u1s1d-m2", extra2, "https://api.u1s1.io/v1",
                        url="https://api.u1s1.io/v1/chat/completions", method="POST")
    assert "x-u1s1-attestation" not in h2 and h2["Authorization"] == "DPoP u1s1d-m2"
