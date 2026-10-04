# -*- coding: utf-8 -*-
"""T-38 KP-P4-06 看板鉴权：TLS+HSTS / 口令 docker secret / 回环绑定。"""
import io
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
AUTH_CONF = ROOT / "docker" / "nginx" / "dashboard_auth.conf"
COMPOSE = ROOT / "docker" / "docker-compose.yml"


def test_auth_conf_uses_tls():
    """看板网关启用 TLS（443 ssl + 证书 + HSTS + 明文HTTP跳转）。"""
    conf = AUTH_CONF.read_text(encoding="utf-8")
    assert "listen 443 ssl" in conf
    assert "ssl_certificate" in conf and "ssl_certificate_key" in conf
    assert "Strict-Transport-Security" in conf  # HSTS


def test_auth_conf_redirects_http_to_https():
    """明文 HTTP(80) 一律 301 跳转 HTTPS，杜绝明文口令。"""
    conf = AUTH_CONF.read_text(encoding="utf-8")
    assert "listen 80" in conf
    assert "return 301 https://" in conf


def test_auth_conf_keeps_strong_auth():
    """网关保留 Basic Auth 强认证。"""
    conf = AUTH_CONF.read_text(encoding="utf-8")
    assert "auth_basic" in conf
    assert "auth_basic_user_file" in conf


def test_compose_password_via_secret_not_env():
    """看板口令经 docker secret 注入，不再经环境变量明文。"""
    d = yaml.safe_load(io.open(COMPOSE, "r", encoding="utf-8"))
    auth = d["services"]["auth"]
    envs = " ".join(str(e) for e in auth.get("environment", []))
    assert "DASHBOARD_PASSWORD" not in envs  # 无明文口令环境变量
    assert "dashboard_auth_password" in auth.get("secrets", [])


def test_compose_top_level_secret_declared():
    """顶层声明 dashboard_auth_password secret（file 引用，不入库）。"""
    d = yaml.safe_load(io.open(COMPOSE, "r", encoding="utf-8"))
    sec = d.get("secrets", {}).get("dashboard_auth_password", {})
    assert "file" in sec


def test_compose_tls_cert_mount_and_loopback():
    """证书卷挂载 + 端口默认回环绑定(127.0.0.1) 且走 443。"""
    d = yaml.safe_load(io.open(COMPOSE, "r", encoding="utf-8"))
    auth = d["services"]["auth"]
    vols = " ".join(str(v) for v in auth.get("volumes", []))
    assert "/etc/nginx/certs" in vols  # TLS 证书卷
    port = str(auth["ports"][0])
    assert "127.0.0.1" in port  # 默认回环绑定
    assert ":443" in port  # HTTPS 端口
