# 文件名：myquant/database/migrations/versions/__init__.py
"""
Alembic 迁移版本目录
====================
存放所有数据库迁移脚本，每个脚本对应一个版本变更。

命名规范：
  - 0001_initial_schema.py      初始建表（8表+索引）
  - 0002_xxx.py                 增量变更（加列/加索引/改表等）

每个迁移脚本包含：
  - revision: 版本ID（字符串）
  - down_revision: 依赖的上一版本
  - upgrade(): 升级函数
  - downgrade(): 回滚函数

使用方式：
  alembic upgrade head    # 升级到最新
  alembic downgrade -1    # 回滚一个版本
  alembic current         # 查看当前版本
  alembic history         # 查看迁移历史
  alembic stamp head      # 标记当前为最新（已有数据库不执行迁移）
"""
