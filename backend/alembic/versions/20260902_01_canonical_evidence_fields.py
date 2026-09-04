"""Rename legacy evidence and silence columns to shared-schema names.

Revision ID: 20260902_01
Revises: None
Create Date: 2026-09-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.database import Base
from app import models  # noqa: F401 - register every shared-schema model


revision: str = "20260902_01"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())

    # This project predates Alembic: its original schema was created by
    # Base.metadata.create_all at application startup. A brand-new managed
    # database therefore needs a real bootstrap before the legacy rename
    # migration can run. Later revisions retain their normal upgrade behavior.
    if "evidence" not in existing_tables:
        Base.metadata.create_all(bind=bind)
        return

    evidence_columns = {column["name"] for column in sa.inspect(bind).get_columns("evidence")}
    if "claim_id" in evidence_columns and "target_id" not in evidence_columns:
        with op.batch_alter_table("evidence") as batch_op:
            batch_op.alter_column(
                "claim_id", existing_type=sa.String(), new_column_name="target_id"
            )
    if "target_type" not in evidence_columns:
        with op.batch_alter_table("evidence") as batch_op:
            batch_op.add_column(sa.Column("target_type", sa.String(), nullable=True))

    unknown_columns = {column["name"] for column in sa.inspect(bind).get_columns("unknowns")}
    if "linked_action_id" in unknown_columns and "source_id" not in unknown_columns:
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
