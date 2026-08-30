"""Associate automations with an optional workspace.

Revision ID: 0012_automation_workspace
Revises: 0011_session_display_title
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_automation_workspace"
down_revision = "0011_session_display_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("automations") as batch_op:
        batch_op.add_column(
            sa.Column("workspace_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_automations_workspace_id",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_automations_workspace_id",
        "automations",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_automations_workspace_id", table_name="automations")
    with op.batch_alter_table("automations") as batch_op:
        batch_op.drop_constraint("fk_automations_workspace_id", type_="foreignkey")
        batch_op.drop_column("workspace_id")
