# 文件名：scripts/台账consumers实测.py
"""
台账 consumers 自动核对薄入口（P2-12，R-18 脚本薄入口纪律）
================================================================
委托 myquant/leakage_gate/static_scan.collect_consumers_by_column 做 AST 实测，
反向聚合"每列数据被哪些策略消费"，打印供与 config/data_time_semantics.yaml
人工填写的 consumers 字段对照，发现漂移。本脚本只读不改写 yaml。

用法：
  python scripts/台账consumers实测.py          # 打印每列实测消费方
  python scripts/台账consumers实测.py --json   # JSON 输出（CI/工具对接）

返回码：0=实测完成（不判定阻断）；1=参数错误。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 保证项目根可导入 myquant（独立运行 / CI 均可用）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    """薄入口：参数探针在此，实测业务逻辑委托 static_scan.collect_consumers_by_column。"""
    from myquant.leakage_gate.static_scan import collect_consumers_by_column

    as_json = "--json" in sys.argv[1:]
    table = collect_consumers_by_column()
    if as_json:
        print(json.dumps(table, ensure_ascii=False, indent=2))
        return 0
    if not table:
        print("（AST 实测：无策略引用台账列，或台账为空）")
        return 0
    print("列名        实测消费策略（AST，供对照 yaml consumers 人工填写）")
    print("-" * 60)
    for col, consumers in table.items():
        print(f"{col:<10} {', '.join(consumers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
