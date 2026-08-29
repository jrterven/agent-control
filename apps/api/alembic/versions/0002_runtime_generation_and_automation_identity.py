"""Bind runtime ids to a connection generation and automation route.

Revision ID: 0002_runtime_identity
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_runtime_identity"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    # SQLite batch mode can leave a temporary table behind when a previous
    # copy failed.  It is Alembic-owned scratch state, never application data.
    if connection.dialect.name == "sqlite":
        tables = set(inspector.get_table_names())
        for canonical, temporary in (
            ("session_links", "_alembic_tmp_session_links"),
            ("automations", "_alembic_tmp_automations"),
        ):
            if temporary in tables and canonical in tables:
                op.drop_table(temporary)
            elif temporary in tables:
                raise RuntimeError(
                    f"Interrupted Alembic batch preserved {temporary}; "
                    f"recover it as {canonical} before retrying the migration"
                )

    # A 0001 runtime id has no process-generation proof and is therefore
    # intentionally invalidated.  This also makes adding the route uniqueness
    # constraint deterministic when legacy rows reused an eight-character id.
    connection.execute(sa.text("UPDATE session_links SET runtime_session_id = NULL"))

    # Keep every local automation record, but detach duplicate upstream claims
    # before adding the authority constraint.  An administrator can explicitly
    # re-import/reconcile detached rows later.
    duplicate_rows = connection.execute(
        sa.text(
            """
            SELECT id, gateway_id, profile_name, hermes_automation_id
            FROM automations
            WHERE hermes_automation_id IS NOT NULL
            ORDER BY created_at, id
            """
        )
    ).mappings()
    seen: set[tuple[str, str, str]] = set()
    for row in duplicate_rows:
        key = (
            str(row["gateway_id"]),
            str(row["profile_name"]),
            str(row["hermes_automation_id"]),
        )
        if key in seen:
            connection.execute(
                sa.text(
                    "UPDATE automations SET hermes_automation_id = NULL WHERE id = :id"
                ),
                {"id": row["id"]},
            )
        else:
            seen.add(key)

    # Existing runtime ids are deliberately left with a NULL generation. They
    # are stale until Hermes confirms them through list/create/resume.
    with op.batch_alter_table("session_links") as batch:
        batch.add_column(sa.Column("runtime_generation", sa.String(length=96), nullable=True))
        batch.create_unique_constraint(
            "uq_session_links_runtime_route",
            ["gateway_id", "profile_name", "runtime_session_id"],
        )
    with op.batch_alter_table("automations") as batch:
        batch.create_unique_constraint(
            "uq_automations_upstream_route",
            ["gateway_id", "profile_name", "hermes_automation_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("automations") as batch:
        batch.drop_constraint("uq_automations_upstream_route", type_="unique")
    with op.batch_alter_table("session_links") as batch:
        batch.drop_constraint("uq_session_links_runtime_route", type_="unique")
        batch.drop_column("runtime_generation")
