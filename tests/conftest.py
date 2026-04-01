# -*- coding: utf-8 -*-
"""
tests/conftest.py — pytest 全局夹具

在任何测试模块被导入之前，向 sys.modules 注入缺失的外部包 stub，
避免"No module named 'jwt' / 'elasticsearch' / ..."类错误。
"""
import os
import sys
import types

# ── 确保项目根目录可被导入 ──────────────────────────────────────────
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 环境变量（在任何 src 模块被导入前设置）──────────────────────────
os.environ.setdefault("JWT_SECRET_KEY",    "test-secret-key-for-unit-tests-only")
os.environ.setdefault("DOUBAO_API_KEY",    "fake-key")
os.environ.setdefault("ADMIN_API_KEY",     "test-admin-key")
os.environ.setdefault("HIKARI_SKILL_KEY",  "test-skill-key")


# ── 缺失外部包 stub ────────────────────────────────────────────────
def _stub(name: str, **attrs):
    """向 sys.modules 注入一个最小 stub 模块，避免 ImportError。"""
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
    return sys.modules[name]


# jwt（PyJWT）
_jwt_stub = _stub("jwt")
# PyJWT 异常类
class _ExpiredSignatureError(Exception): pass
class _InvalidTokenError(Exception): pass
_jwt_stub.ExpiredSignatureError = _ExpiredSignatureError
_jwt_stub.InvalidTokenError = _InvalidTokenError

def _jwt_encode(payload, secret, algorithm="HS256"):
    """最小 JWT stub：base64url(header).base64url(payload).sig"""
    import base64, json, hashlib, hmac
    from datetime import datetime

    def _default(obj):
        if isinstance(obj, datetime):
            return obj.timestamp()
        return str(obj)

    def b64(d):
        return base64.urlsafe_b64encode(
            json.dumps(d, default=_default).encode()
        ).rstrip(b"=").decode()

    header = b64({"alg": algorithm, "typ": "JWT"})
    body   = b64(payload)
    sig    = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"

def _jwt_decode(token, secret, algorithms=None, options=None):
    import base64, json, hashlib, hmac
    parts = token.split(".")
    if len(parts) != 3:
        raise _InvalidTokenError("Bad token format")
    header_b64, body_b64, sig_b64 = parts
    # 验证签名
    expected_sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"{header_b64}.{body_b64}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    if not hmac.compare_digest(sig_b64, expected_sig):
        raise _InvalidTokenError("Signature verification failed")
    # 解码 payload
    body = body_b64 + "=" * (-len(body_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(body))
    # 过期检查（除非 verify_exp=False）
    verify_exp = True
    if options and options.get("verify_exp") is False:
        verify_exp = False
    if verify_exp:
        from datetime import datetime, timezone
        exp = payload.get("exp")
        if exp is not None:
            now = datetime.now(timezone.utc).timestamp()
            if now > exp:
                raise _ExpiredSignatureError("Token expired")
    return payload

_jwt_stub.encode = _jwt_encode
_jwt_stub.decode = _jwt_decode

# elasticsearch
_es_stub = _stub("elasticsearch")
class _AsyncElasticsearch:
    def __init__(self, *a, **kw): pass
_es_stub.AsyncElasticsearch = _AsyncElasticsearch
_stub("elasticsearch.helpers")

# bson
_bson_stub = _stub("bson")
class _ObjectId(str): pass
_bson_stub.ObjectId = _ObjectId

# motor
_motor_stub       = _stub("motor")
_motor_async_stub = _stub("motor.motor_asyncio")
class _AsyncIOMotorClient:
    def __init__(self, *a, **kw): pass
_motor_async_stub.AsyncIOMotorClient = _AsyncIOMotorClient

# redis（aioredis via redis.asyncio）
_redis_stub        = _stub("redis")
_redis_asyncio_stub = _stub("redis.asyncio")

# pymongo
_pymongo_stub = _stub("pymongo")
_pymongo_stub.ASCENDING  = 1
_pymongo_stub.DESCENDING = -1
_pymongo_stub.TEXT = "$text"
_stub("pymongo.errors")
_pymongo_errors = sys.modules["pymongo.errors"]
class _OperationFailure(Exception): pass
_pymongo_errors.OperationFailure = _OperationFailure

# volcenginesdkarkruntime（豆包 SDK）
_ark_stub = _stub("volcenginesdkarkruntime")
class _Ark:
    def __init__(self, *a, **kw):
        self.multimodal_embeddings = types.SimpleNamespace(create=lambda *a, **kw: None)
_ark_stub.Ark = _Ark

# qdrant_client（避免 src.utils.qdrant_client 的依赖）
_qc_stub = _stub("qdrant_client")
class _QdrantClient:
    def __init__(self, *a, **kw): pass
_qc_stub.QdrantClient = _QdrantClient

_qc_http_stub = _stub("qdrant_client.http")
_qc_models_stub = _stub("qdrant_client.http.models")
# 注入 qdrant_client.http.models 所需的类/枚举 stub
class _Distance:
    COSINE = "Cosine"
    DOT    = "Dot"
    EUCLID = "Euclid"
class _VectorParams:
    def __init__(self, *a, **kw): pass
class _PointStruct:
    def __init__(self, *a, **kw): pass
class _Filter:
    def __init__(self, *a, **kw): pass
class _FieldCondition:
    def __init__(self, *a, **kw): pass
class _MatchValue:
    def __init__(self, *a, **kw): pass
_qc_models_stub.Distance       = _Distance
_qc_models_stub.VectorParams   = _VectorParams
_qc_models_stub.PointStruct    = _PointStruct
_qc_models_stub.Filter         = _Filter
_qc_models_stub.FieldCondition = _FieldCondition
_qc_models_stub.MatchValue     = _MatchValue

_qc_exc_stub = _stub("qdrant_client.http.exceptions")
class _UnexpectedResponse(Exception): pass
_qc_exc_stub.UnexpectedResponse = _UnexpectedResponse

# openai
_openai_stub = _stub("openai")
class _OpenAI:
    def __init__(self, *a, **kw): pass
class _AsyncOpenAI:
    def __init__(self, *a, **kw): pass
class _RateLimitError(Exception): pass
class _APIStatusError(Exception): pass
_openai_stub.OpenAI         = _OpenAI
_openai_stub.AsyncOpenAI    = _AsyncOpenAI
_openai_stub.RateLimitError = _RateLimitError
_openai_stub.APIStatusError = _APIStatusError

# aiohttp
_stub("aiohttp")

# python-magic
_magic_stub = _stub("magic")
class _Magic:
    def __init__(self, *a, **kw): pass
    def from_buffer(self, *a, **kw): return "image/jpeg"
_magic_stub.Magic = _Magic

# Pillow
_pil_stub       = _stub("PIL")
_pil_image_stub = _stub("PIL.Image")
class _PILImage:
    @staticmethod
    def open(*a, **kw): return types.SimpleNamespace(size=(100,100), mode="RGB", save=lambda *a,**kw: None)
_pil_image_stub.Image = _PILImage
_pil_stub.Image = _pil_image_stub
