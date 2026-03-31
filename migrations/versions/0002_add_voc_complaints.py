"""Add voc_complaints table for VOC-to-case linkage.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-01 00:00:00.000000

New table:
  voc_complaints — maps VOC (Voice of Customer) complaint numbers to cases.
                   Populated by the fetch_voc ingestion job.

New enum:
  voc_match_status_enum — matched / unmatched / ambiguous
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as pg_enum

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    bind = op.get_bind()

    # New enum type
    sa.Enum("matched", "unmatched", "ambiguous",
            name="voc_match_status_enum").create(bind, checkfirst=True)

    op.create_table(
        "voc_complaints",
        sa.Column("id",              sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("voc_number",      sa.BigInteger(), nullable=False),
        sa.Column("case_id",         sa.BigInteger(), nullable=True),
        sa.Column("state_id",        sa.Integer(),    nullable=True),
        sa.Column("court_name",      sa.String(255),  nullable=True),
        sa.Column("case_number_raw", sa.String(100),  nullable=True),
        sa.Column("match_status",    pg_enum("matched", "unmatched", "ambiguous",
                                             name="voc_match_status_enum",
                                             create_type=False),
                  nullable=False, server_default="unmatched"),
        sa.Column("raw_payload",     sa.Text(),       nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id",         name="pk_voc_complaints"),
        sa.UniqueConstraint("voc_number",     name="uq_voc_number"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"],
                                name="fk_voc_complaints_case", ondelete="SET NULL"),
    )

    op.create_index("idx_voc_case_id",      "voc_complaints", ["case_id"])
    op.create_index("idx_voc_match_status", "voc_complaints", ["match_status"])
    op.create_index("idx_voc_state_id",     "voc_complaints", ["state_id"])

    # updated_at trigger (same pattern as 0001)
    op.execute("""
        CREATE TRIGGER trg_voc_complaints_updated_at
        BEFORE UPDATE ON voc_complaints
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_voc_complaints_updated_at ON voc_complaints;")
    op.drop_table("voc_complaints")
    op.execute("DROP TYPE IF EXISTS voc_match_status_enum;")
