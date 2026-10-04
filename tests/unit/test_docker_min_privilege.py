# 文件名：tests/unit/test_docker_min_privilege.py
"""
test_docker_min_privilege.py - 镜像构建与最小权限测试（KP-M01 / T-31）
========================================================================
覆盖修复：
  1. 根级 .dockerignore 排除敏感数据/密钥目录、保留 .env.example。
  2. Dockerfile 非 root 运行（USER appuser）且 pip 安装去除 dev extras。
  3. docker-compose live 探针从 pgrep 进程匹配改业务健康接口（live_status.json 新鲜度）。

依赖: pytest
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent.parent


class TestDockerIgnore:
    def test_排除敏感目录(self) -> None:
        """DoD：.dockerignore 排除 .env/数据库/实盘状态/输出等敏感目录。"""
        text = (_PROJ / ".dockerignore").read_text(encoding="utf-8")
        for pat in (".env", "database/", "实盘状态/", "实盘信号/", "outputs/", ".git/", "backups/"):
            assert pat in text, f".dockerignore 缺失排除项: {pat}"

    def test_保留env_example(self) -> None:
        """DoD：.dockerignore 保留 .env.example（模板无真实密钥）。"""
        text = (_PROJ / ".dockerignore").read_text(encoding="utf-8")
        assert "!.env.example" in text


class TestDockerfile:
    def test_非root运行(self) -> None:
        """DoD：Dockerfile 创建 appuser 并切换非 root 用户。"""
        text = (_PROJ / "docker" / "Dockerfile").read_text(encoding="utf-8")
        assert "useradd" in text and "appuser" in text
        assert "USER appuser" in text

    def test_pip去dev(self) -> None:
        """DoD：pip install 用 .[live,web]，不装 dev extras。"""
        text = (_PROJ / "docker" / "Dockerfile").read_text(encoding="utf-8")
        assert ".[live,web]" in text
        assert "live,dev,web" not in text


class TestComposeLiveProbe:
    def test_探针改业务健康接口(self) -> None:
        """DoD：live 容器探针不再用 pgrep 进程匹配，改校验 live_status.json 新鲜度。"""
        text = (_PROJ / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
        assert '"CMD-SHELL", "pgrep' not in text
        assert "live_status.json" in text

    def test_业务探针逻辑(self, tmp_path) -> None:
        """DoD：live_status.json 新鲜（mtime<30s）判健康，过期判不健康。"""
        probe = (
            "import os,time,sys; "
            "p=sys.argv[1]; "
            "sys.exit(0 if os.path.isfile(p) and time.time()-os.path.getmtime(p)<30 else 1)"
        )
        fresh = tmp_path / "live_status.json"
        fresh.write_text("{}", encoding="utf-8")
        stale = tmp_path / "stale.json"
        stale.write_text("{}", encoding="utf-8")
        old = time.time() - 100
        os.utime(stale, (old, old))
        py = "D:/ProgramData/anaconda3/envs/conda_wdh/python.exe"
        import subprocess
        assert subprocess.run([py, "-c", probe, str(fresh)], capture_output=True).returncode == 0
        assert subprocess.run([py, "-c", probe, str(stale)], capture_output=True).returncode == 1
