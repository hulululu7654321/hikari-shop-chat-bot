# -*- coding: utf-8 -*-
"""
tests/test_auth.py — JWT 鉴权函数单测

覆盖（纯函数，不需要 Redis / DB）：
  create_access_token()   签发
  verify_access_token()   验签（有效/过期/篡改/格式错误）
  decode_token_payload()  不校验过期的解码
  _get_secret()           配置校验
"""

import os
import pytest
from unittest.mock import patch


# ══════════════════════════════════════════════════════════════════
# create_access_token + verify_access_token
# ══════════════════════════════════════════════════════════════════

class TestCreateAndVerifyAccessToken:

    def test_returns_token_and_jti(self):
        from src.utils.auth import create_access_token
        token, jti = create_access_token("user123")
        assert isinstance(token, str)
        assert isinstance(jti, str)
        assert len(jti) > 0

    def test_verify_valid_token(self):
        from src.utils.auth import create_access_token, verify_access_token
        token, jti = create_access_token("user_abc")
        result = verify_access_token(token)
        assert result is not None
        assert result["user_id"] == "user_abc"
        assert result["jti"] == jti

    def test_verify_vip_level_default(self):
        from src.utils.auth import create_access_token, verify_access_token
        token, _ = create_access_token("uid")
        result = verify_access_token(token)
        assert result["vip_level"] == 0

    def test_verify_vip_level_custom(self):
        from src.utils.auth import create_access_token, verify_access_token
        token, _ = create_access_token("uid", vip_level=2)
        result = verify_access_token(token)
        assert result["vip_level"] == 2

    def test_verify_role_none_default(self):
        from src.utils.auth import create_access_token, verify_access_token
        token, _ = create_access_token("uid")
        result = verify_access_token(token)
        assert result["role"] is None

    def test_verify_role_admin(self):
        from src.utils.auth import create_access_token, verify_access_token
        token, _ = create_access_token("uid", role="admin")
        result = verify_access_token(token)
        assert result["role"] == "admin"

    def test_verify_expired_token_returns_none(self):
        from src.utils.auth import create_access_token, verify_access_token
        import src.utils.auth as auth_mod
        # TTL 设为负数 → exp < iat → 签出来就是过期的
        with patch.object(auth_mod, "_ACCESS_TOKEN_EXPIRE_SECONDS", -3600):
            token, _ = create_access_token("uid_exp")
        result = verify_access_token(token)
        assert result is None

    def test_verify_tampered_token_returns_none(self):
        from src.utils.auth import create_access_token, verify_access_token
        token, _ = create_access_token("uid")
        # 篡改 token 最后几个字符
        tampered = token[:-4] + "xxxx"
        result = verify_access_token(tampered)
        assert result is None

    def test_verify_garbage_token_returns_none(self):
        from src.utils.auth import verify_access_token
        result = verify_access_token("this.is.garbage")
        assert result is None

    def test_verify_empty_string_returns_none(self):
        from src.utils.auth import verify_access_token
        result = verify_access_token("")
        assert result is None

    def test_jti_is_unique_across_calls(self):
        from src.utils.auth import create_access_token
        _, jti1 = create_access_token("uid")
        _, jti2 = create_access_token("uid")
        assert jti1 != jti2


# ══════════════════════════════════════════════════════════════════
# decode_token_payload（不校验过期）
# ══════════════════════════════════════════════════════════════════

class TestDecodeTokenPayload:

    def test_decode_valid_token(self):
        from src.utils.auth import create_access_token, decode_token_payload
        token, _ = create_access_token("uid_decode")
        payload = decode_token_payload(token)
        assert payload is not None
        assert payload["sub"] == "uid_decode"

    def test_decode_expired_token_still_returns_payload(self):
        from src.utils.auth import create_access_token, decode_token_payload
        import src.utils.auth as auth_mod
        with patch.object(auth_mod, "_ACCESS_TOKEN_EXPIRE_SECONDS", -3600):
            token, _ = create_access_token("uid_exp")
        result = decode_token_payload(token)
        # 不校验过期，应该能解码成功
        assert result is not None
        assert result["sub"] == "uid_exp"

    def test_decode_invalid_token_returns_none(self):
        from src.utils.auth import decode_token_payload
        result = decode_token_payload("invalid.token.here")
        assert result is None


# ══════════════════════════════════════════════════════════════════
# _get_secret 配置校验
# ══════════════════════════════════════════════════════════════════

class TestGetSecret:
    def test_valid_secret_returned(self):
        from src.utils.auth import _get_secret
        # conftest 已设置 JWT_SECRET_KEY
        secret = _get_secret()
        assert isinstance(secret, str)
        assert len(secret) > 0

    def test_empty_secret_raises(self):
        from src.utils.auth import _get_secret
        with patch.dict(os.environ, {"JWT_SECRET_KEY": ""}):
            with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
                _get_secret()

    def test_fill_in_secret_raises(self):
        from src.utils.auth import _get_secret
        with patch.dict(os.environ, {"JWT_SECRET_KEY": "FILL_IN_YOUR_SECRET"}):
            with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
                _get_secret()
