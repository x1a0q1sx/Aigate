"""u1s1 客户端信号：RFC 9449 DPoP proof（EC P-256 / ES256）逐请求签发。

对齐官方 CLI device-auth.js 的实测行为：
  Authorization: DPoP <device_token(u1s1d-…)>
  DPoP: <header>.<payload>.<sig>   typ=dpop+jwt, ES256, header 内嵌设备公钥 JWK
  payload: jti(hex32), htm, htu(origin+path 去 query/hash), iat, ath=b64url(sha256(device_token))
签名是 JWS 的 raw r||s（64 字节），非 DER。
纯函数，不触库不发请求；私钥由 oauth_tokens.device_key_enc 提供。
"""
from __future__ import annotations
import base64
import hashlib
import json
import time
import uuid
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives import hashes

_ENC = "utf-8"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_int(s: str) -> int:
    pad = "=" * (-len(s) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(s + pad), "big")


def _coord(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()


def private_key_from_jwk(jwk: dict) -> ec.EllipticCurvePrivateKey:
    nums = ec.EllipticCurvePrivateNumbers(
        private_value=_b64u_int(jwk["d"]),
        public_numbers=ec.EllipticCurvePublicNumbers(
            x=_b64u_int(jwk["x"]), y=_b64u_int(jwk["y"]), curve=ec.SECP256R1(),
        ),
    )
    return nums.private_key()


def public_jwk_from(private: ec.EllipticCurvePrivateKey) -> dict:
    pn = private.public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256", "x": _coord(pn.x), "y": _coord(pn.y), "ext": True}


def _clean_htu(url: str) -> str:
    sp = urlsplit(url)
    path = sp.path or "/"
    return f"{sp.scheme}://{sp.netloc}{path}"


def sign_proof(private_jwk: dict, bearer_token: str, method: str, url: str,
               now: int | None = None) -> dict:
    """签发一枚 DPoP proof，返回需覆盖到出站请求上的头（authorization/dpop）。"""
    priv = private_key_from_jwk(private_jwk)
    header = _b64u(json.dumps(
        {"typ": "dpop+jwt", "alg": "ES256", "jwk": public_jwk_from(priv)},
        separators=(",", ":")).encode(_ENC))
    ath = _b64u(hashlib.sha256(bearer_token.encode(_ENC)).digest())
    payload = _b64u(json.dumps(
        {"jti": uuid.uuid4().hex, "htm": (method or "POST").upper(), "htu": _clean_htu(url),
         "iat": int(now if now is not None else time.time()), "ath": ath},
        separators=(",", ":")).encode(_ENC))
    signing_input = f"{header}.{payload}".encode(_ENC)
    der = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return {"Authorization": f"DPoP {bearer_token}", "DPoP": f"{header}.{payload}.{sig}"}
