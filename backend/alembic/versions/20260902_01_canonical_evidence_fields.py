"""Rename legacy evidence and silence columns to shared-schema names.

Revision ID: 20260902_01
Revises: None
Create Date: 2026-09-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260902_01"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.alter_column(
            "claim_id", existing_type=sa.String(), new_column_name="target_id"
        )
        batch_op.add_column(sa.Column("target_type", sa.String(), nullable=True))
    with op.batch_alter_table("unknowns") as batch_op:
        batch_op.alter_column(
            "linked_action_id", existing_type=sa.String(), new_column_name="source_id"
        )


def downgrade() -> None:
    with op.batch_alter_table("unknowns") as batch_op:
        batch_op.alter_column(
            "source_id", existing_type=sa.String(), new_column_name="linked_action_id"
        )
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.drop_column("target_type")
        batch_op.alter_column(
            "target_id", existing_type=sa.String(), new_column_name="claim_id"
        )
