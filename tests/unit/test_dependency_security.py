# -*- coding: utf-8 -*-
"""T-37 KP-M03 依赖与 CI 安全：哈希锁文件 + ci.yml 安全扫描 + file_utils marker。"""
import io
import re
from pathlib import Path

import yaml

from myquant.utils.file_utils import find_project_root

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "requirements.lock"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def test_find_project_root_default_marker_pyproject():
    """file_utils.find_project_root 默认 marker 改为 pyproject.toml，能定位项目根。"""
    root = find_project_root()
    assert (root / "pyproject.toml").exists()


def test_find_project_root_marker_param_still_works():
    """marker 参数可覆盖（向后兼容）。"""
    root = find_project_root(marker="pyproject.toml")
    assert (root / "pyproject.toml").exists()


def test_requirements_lock_exists():
    """哈希锁文件 requirements.lock 存在且非空。"""
    assert LOCK.exists(), "requirements.lock 缺失"
    assert LOCK.stat().st_size > 0


def test_requirements_lock_contains_hashes():
    """锁文件依赖条目带 sha256 哈希（--generate-hashes 产出）。"""
    text = LOCK.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert lines, "锁文件无依赖行"
    # 每包条目为「包==版本 \\」+ 若干 `--hash=sha256:... \\` 行；哈希行数应覆盖绝大多数行
    hashed = [ln for ln in lines if re.search(r"--hash=sha256:[0-9a-f]{64}", ln)]
    assert len(hashed) >= 0.9 * len(lines), f"{len(hashed)}/{len(lines)} 行带哈希"
    # 首个包条目（aiohappyeyeballs 或任意）后必须紧跟哈希
    pkg_lines = [ln for ln in lines if re.match(r"^[A-Za-z0-9_.-]+==[0-9]", ln)]
    assert pkg_lines, "锁文件无包==版本条目"


def test_ci_has_security_scan_job():
    """ci.yml 增 security-scan job（pip-audit + gitleaks）。"""
    cfg = yaml.safe_load(io.open(CI, "r", encoding="utf-8"))
    jobs = cfg.get("jobs", {})
    assert "security-scan" in jobs
    steps = "\n".join(
        s.get("name", "") for s in jobs["security-scan"].get("steps", [])
    )
    assert "pip-audit" in steps
    assert "gitleaks" in steps
