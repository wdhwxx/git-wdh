# 文件名：tests/unit/test_env_auth_chain.py
"""
test_env_auth_chain.py - .env与认证链路未连通测试（KP-P1-04 / T-22）
=====================================================================
覆盖修复：
  1. QUANT_API_KEYS 环境变量生效（auth 直接读取）。
  2. 环境变量缺失时 auth 经 settings（.env/config）兜底生效。
  3. 环境变量优先于 settings（同源键按优先级）。
  4. 运行API.py 业务导入前显式 get_settings()（子进程 --help 正常，bootstrap 不崩）。

依赖: pytest, myquant.api.middleware.auth, myquant.core.settings
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from myquant.api.middleware.auth import APIKeyAuth
from myquant.core.settings import get_settings

_PROJ = Path(__file__).resolve().parent.parent.parent


class TestAuthEnvChain:
    def test_env_keys_生效(self, monkeypatch) -> None:
        """DoD：环境变量 QUANT_API_KEYS 配置的密钥生效。"""
        monkeypatch.setenv("QUANT_API_KEYS", "k1,k2")
        auth = APIKeyAuth(enabled=True)
        assert auth.key_count == 2
        assert auth.verify("k1") is True
        assert auth.verify("k3") is False

    def test_settings_兜底生效(self, monkeypatch) -> None:
        """DoD：无环境变量时，auth 经 settings 兜底读取密钥生效。"""
        monkeypatch.delenv("QUANT_API_KEYS", raising=False)
        settings = get_settings()
        settings.set("QUANT_API_KEYS", "sk_from_settings")
        try:
            auth = APIKeyAuth(enabled=True)
            assert auth.verify("sk_from_settings") is True
        finally:
            settings.set("QUANT_API_KEYS", None)

    def test_env_优先于settings(self, monkeypatch) -> None:
        """DoD：环境变量优先于 settings（同键）。"""
        monkeypatch.setenv("QUANT_API_KEYS", "env_key")
        settings = get_settings()
        settings.set("QUANT_API_KEYS", "set_key")
        try:
            auth = APIKeyAuth(enabled=True)
            assert auth.verify("env_key") is True
            assert auth.verify("set_key") is False
        finally:
            settings.set("QUANT_API_KEYS", None)


class TestRunApiBootstrap:
    def test_bootstrap_不崩(self) -> None:
        """DoD：运行API.py 业务导入前显式 get_settings()，--help 正常退出（集成）。"""
        script = _PROJ / "scripts" / "运行API.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(_PROJ),
        )
        assert result.returncode == 0
