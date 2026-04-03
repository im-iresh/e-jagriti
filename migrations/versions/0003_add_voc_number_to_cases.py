"""Add voc_number column to cases for fast no-VOC alert queries.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-04 00:00:00.000000

Change:
  cases.voc_number — BigInteger, nullable, unique.
  Mirrors voc_complaints.voc_number for the matched case.
  NULL means no VOC complaint is linked to this case.

  Partial index idx_cases_no_voc on (id) WHERE voc_number IS NULL
  makes the alert query "cases with no VOC" an index scan.

  Backfill copies voc_number from existing voc_complaints rows where
  a case_id match exists.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    op.add_column("cases", sa.Column("voc_number", sa.BigInteger(), nullable=True))
    op.create_unique_constraint("uq_cases_voc_number", "cases", ["voc_number"])
    op.create_index(
        "idx_cases_no_voc",
        "cases",
        ["id"],
        postgresql_where=sa.text("voc_number IS NULL"),
    )

    # Backfill: copy voc_number from matched voc_complaints rows
    op.execute("""
        UPDATE cases
           SET voc_number = v.voc_number
          FROM voc_complaints v
         WHERE v.case_id = cases.id
    """)


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    op.drop_index("idx_cases_no_voc", table_name="cases")
    op.drop_constraint("uq_cases_voc_number", "cases", type_="unique")
    op.drop_column("cases", "voc_number")
