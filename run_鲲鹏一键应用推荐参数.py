# -*- coding: utf-8 -*-
"""
run_鲲鹏一键应用推荐参数.py
============================================
把"固定参数"一键应用到 config/backtest_kunpeng.yaml 并跑全池批量回测验证。

设计目标（参数固定，便于日后删除）:
  - 推荐参数以常量 FIXED_PARAMS 内嵌本脚本（取自 2026-09-18 outputs/kunpeng_param_opt/
    summary.json 的 recommended），不依赖优化脚本及其 JSON 产物，脚本为自包含单文件；
  - 写回采用"文本段替换"：只更新 strategy.params 段的参数值，保留配置注释及其它内容；
  - 参数定稿后，本脚本与 run_鲲鹏参数优化.py 均可直接删除，不影响 run_鲲鹏批量回测.py 日常使用。

用法（需在 quant 环境运行）:
  D:\\ProgramData\\anaconda3\\envs\\quant\\python.exe run_鲲鹏一键应用推荐参数.py
  python run_鲲鹏一键应用推荐参数.py --dry-run        # 仅打印将写入的差异，不改文件
  python run_鲲鹏一键应用推荐参数.py --no-backtest    # 只写回配置，不跑批量回测
  python run_鲲鹏一键应用推荐参数.py --refine-top 50  # 透传批量回测阶段二 top N

退出码: 0=成功；1=参数错误/未改动；2=运行失败
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "backtest_kunpeng.yaml"
BACKUP_DIR = PROJECT_ROOT / "backups"

# ── 固定参数（第二轮最优，来源：outputs/kunpeng_param_opt_loose/summary.json round2_best） ──
FIXED_PARAMS: Dict[str, Any] = {
    "vol_ratio_threshold": 2.0,
    "vol_count_window": 6,
    "vol_active_days": 3,
    "barslast_vol_window": 13,
    "barslast_ratio_window": 15,
    "ma_sticky_threshold": 1.15,
    "diff_dea_ratio": 0.01,
    "macd_abs_threshold": 0.1,
    "macd_rise_threshold": 0.00002,
    "exit_mode": "aggressive",
    "hold_days_cap": 90,
    "max_position_pct": 0.3,
    "max_positions": 5,
    "capital_base": 1_000_000.0,
}

PARAM_VALUE_RE = re.compile(r"^(    )(?P<key>[a-zA-Z_][\w]*)(:)(?P<sep>\s*)(?P<val>.*)$")


def fmt_value(v: Any) -> str:
    """把参数值格式化为配置文件的 YAML 标量文本。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return f"{v:.10f}".rstrip("0").rstrip(".")
    return str(v)


def update_params_text(text: str, params: Dict[str, Any]) -> str:
    """仅替换 strategy.params 段内的参数值，其余文本（注释/格式）原样保留。"""
    lines = text.split("\n")
    out: list[str] = []
    in_params = False
    for line in lines:
        if re.match(r"^strategy:\s*$", line):
            in_params = False
        elif re.match(r"^  params:\s*$", line):
            in_params = True
        elif in_params and line and not line.startswith("    ") and not line.startswith("#"):
            # 遇到下一个顶层键（如 sim_broker:）即离开 params 段
            in_params = False
        if in_params and line.startswith("    ") and not line.lstrip().startswith("#"):
            m = PARAM_VALUE_RE.match(line)
            if m and m.group("key") in params:
                line = f"{m.group(1)}{m.group('key')}{m.group(3)} {fmt_value(params[m.group('key')])}"
        out.append(line)
    return "\n".join(out)


def load_current_params(text: str) -> Dict[str, str]:
    """解析当前 strategy.params 段，返回 {key: 原值文本}（仅用于对照展示）。"""
    found: Dict[str, str] = {}
    in_params = False
    for line in text.split("\n"):
        if re.match(r"^strategy:\s*$", line):
            in_params = False
        elif re.match(r"^  params:\s*$", line):
            in_params = True
        elif in_params and line and not line.startswith("    ") and not line.startswith("#"):
            in_params = False
        if in_params:
            m = PARAM_VALUE_RE.match(line)
            if m:
                found[m.group("key")] = m.group("val").strip()
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run_鲲鹏一键应用推荐参数",
        description="把固定参数写回 config/backtest_kunpeng.yaml 并跑全池批量回测验证",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印差异，不写文件、不跑回测")
    parser.add_argument("--no-backtest", action="store_true", help="只写回配置，不跑批量回测")
    parser.add_argument("--refine-top", type=int, default=30, help="透传批量回测阶段二 top N")
    args = parser.parse_args()

    if not CONFIG_PATH.is_file():
        print(f"[错误] 配置文件不存在: {CONFIG_PATH}")
        return 1

    text = CONFIG_PATH.read_text(encoding="utf-8")
    old = load_current_params(text)
    new_text = update_params_text(text, FIXED_PARAMS)

    changed = {k: (old.get(k), fmt_value(v)) for k, v in FIXED_PARAMS.items()
               if old.get(k) != fmt_value(v)}
    if args.dry_run:
        print("=" * 72)
        print("  DRY-RUN：以下参数将被写入 config/backtest_kunpeng.yaml（未实际修改）")
        print("=" * 72)
        if not changed:
            print("  当前配置与固定参数完全一致，无需改动。")
        for k, (o, n) in changed.items():
            print(f"  {k:<24} {str(o):>14}  →  {n}")
        if not changed:
            return 0
        print("\n  提示：去掉 --dry-run 后运行即执行备份→写回→批量回测。")
        return 0

    if changed:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = BACKUP_DIR / f"backtest_kunpeng_{stamp}.yaml"
        shutil.copy2(CONFIG_PATH, backup_path)
        print(f"[备份] {backup_path}")
        CONFIG_PATH.write_text(new_text, encoding="utf-8")
        print("=" * 72)
        print("  已写回以下参数:")
        print("=" * 72)
        for k, (o, n) in changed.items():
            print(f"  {k:<24} {str(o):>14}  →  {n}")
    else:
        print("[提示] 当前配置与固定参数完全一致，无需写回。")

    if args.no_backtest:
        print("\n[完成] 已按 --no-backtest 跳过批量回测。")
        return 0

    # ── 调用全池批量回测（指纹缓存含参数，参数变化会自动重算） ──
    bt_script = PROJECT_ROOT / "run_鲲鹏批量回测.py"
    if not bt_script.is_file():
        print(f"[警告] 未找到 {bt_script.name}，跳过批量回测。")
        return 0
    cmd = [sys.executable, str(bt_script), "--refine-top", str(args.refine_top)]
    print(f"\n[回测] 启动全池批量回测（python={sys.executable}）...")
    try:
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 批量回测启动失败: {exc}")
        return 2
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
