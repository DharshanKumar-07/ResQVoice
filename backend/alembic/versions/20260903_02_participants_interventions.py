"""Persist Agora participant identities and intervention lifecycle records.

Revision ID: 20260903_02
Revises: 20260902_01
Create Date: 2026-09-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260903_02"
down_revision: Union[str, Sequence[str], None] = "20260902_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "participants" not in existing_tables:
        op.create_table(
        "participants",
        sa.Column("agora_uid", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("participant_type", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("agora_uid", "channel"),
        )
    if "interventions" not in existing_tables:
        op.create_table(
        "interventions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("trigger_type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("related_claim_ids", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("spoken_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        )
    intervention_indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("interventions")
    }
    if "ix_interventions_trigger_type" not in intervention_indexes:
        op.create_index("ix_interventions_trigger_type", "interventions", ["trigger_type"])
    if "ix_interventions_status" not in intervention_indexes:
        op.create_index("ix_interventions_status", "interventions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_interventions_status", table_name="interventions")
    op.drop_index("ix_interventions_trigger_type", table_name="interventions")
    op.drop_table("interventions")
    op.drop_table("participants")
