# 文件名：myquant/database/migrations/versions/${up_revision}.py
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """升级：执行本版本的结构变更"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """回滚：撤销本版本的结构变更"""
    ${downgrades if downgrades else "pass"}
