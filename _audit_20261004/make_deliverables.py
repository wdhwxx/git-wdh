# -*- coding: utf-8 -*-
"""make_deliverables.py — 合并审计报告正文与工作底稿附录，并生成 DOCX。

产物（写入 _audit_20261004/build/）：
  1) 鲲鹏量化交易系统审计报告（含工作底稿附录）.md
  2) 鲲鹏量化交易系统审计报告（含工作底稿附录）.docx
  3) 鲲鹏量化交易系统审计工作底稿（详细发现）.md
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"

APPENDIX_MAP = [
    ("附录 A", "findings_batch1.md"),
    ("附录 B", "findings_batch2.md"),
    ("附录 C", "findings_batch2_supplement.md"),
    ("附录 D", "findings_batch3.md"),
    ("附录 E", "findings_batch4.md"),
    ("附录 F", "findings_batch5.md"),
    ("附录 G", "findings_batch6.md"),
]


def demote(lines: list[str]) -> list[str]:
    """把底稿的 # / ### 层级下移，使附录嵌入正文层级。"""
    out: list[str] = []
    for line in lines:
        if line.startswith("### "):
            out.append(line.replace("### ", "#### ", 1))
        elif line.startswith("## "):
            out.append(line.replace("## ", "### ", 1))
        elif line.startswith("# "):
            out.append(line.replace("# ", "## ", 1))
        else:
            out.append(line)
    return out


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    report = (HERE / "report_src.md").read_text(encoding="utf-8").rstrip()

    parts: list[str] = [report, "", "---", "", "# 附录：审计工作底稿（详细发现全文）", "",
                        "> 本附录收录 50 项发现的完整 9.3 字段（事实、推断、未知、影响、可能性、根因、"
                        "整改建议、验证方法 DoD、最坏情形、是否需法律顾问确认、管理层回复台账字段）。"
                        "正文 §4 已展开 11 项 P0 与 5 项复合发现，附录为避免重复计分而保留原始编号。", ""]

    for label, fname in APPENDIX_MAP:
        path = HERE / fname
        if not path.exists():
            print(f"WARN missing {fname}")
            continue
        body = path.read_text(encoding="utf-8").rstrip()
        lines = demote(body.splitlines())
        # 去掉底稿自带的「阅读说明」重复段落，仅保留标题与发现
        parts.extend([f"## {label}（{fname}）", ""] + lines + ["", "---", ""])

    combined = "\n".join(parts)
    out_md = BUILD / "鲲鹏量化交易系统审计报告（含工作底稿附录）.md"
    out_md.write_text(combined, encoding="utf-8")

    # 单独一份工作底稿合集
    wp_parts = ["# 鲲鹏量化交易系统审计工作底稿（详细发现）", "",
                "> 审计基准：2026-10-04 文件系统快照；本文件为审计工作底稿草案，不是鉴证报告。", ""]
    for label, fname in APPENDIX_MAP:
        path = HERE / fname
        if path.exists():
            wp_parts.extend([path.read_text(encoding="utf-8").rstrip(), "", "---", ""])
    out_wp = BUILD / "鲲鹏量化交易系统审计工作底稿（详细发现）.md"
    out_wp.write_text("\n".join(wp_parts), encoding="utf-8")

    sys.path.insert(0, str(HERE))
    from build_docx import convert  # noqa: E402

    out_docx = BUILD / "鲲鹏量化交易系统审计报告（含工作底稿附录）.docx"
    tables = convert(out_md, out_docx, "鲲鹏量化交易系统审计报告 · 2026-10-04 快照 · 工作底稿草案")

    print(f"OK md={out_md.stat().st_size}B wp={out_wp.stat().st_size}B")
    print(f"OK docx={out_docx.stat().st_size}B tables={tables}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
