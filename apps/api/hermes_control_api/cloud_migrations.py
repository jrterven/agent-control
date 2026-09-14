"""Validate a new release's migrations on an operator-created restored database."""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from .config import get_settings


def alembic_config_path() -> Path:
    # Container operator commands import the installed wheel, while Alembic's
    # scripts/config live in the source tree copied into the image.
    candidates = (Path.cwd() / "apps/api/alembic.ini", Path(__file__).resolve().parents[1] / "alembic.ini")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit("Alembic configuration not found; run from the release root")


def restored_database_url(database_url: str, database: str):
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise SystemExit("PostgreSQL required")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return url.set(database=database)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    args = parser.parse_args()
    if not re.fullmatch(r"control_release_check_[a-z0-9_]{1,50}", args.database):
        raise SystemExit("Only isolated release-check databases are accepted")
    settings = get_settings()
    if settings.deployment_mode != "cloud":
        raise SystemExit("Cloud mode required")
    url = restored_database_url(settings.database_url, args.database)
    engine = create_engine(url)
    tables = inspect(engine).get_table_names()
    if "alembic_version" not in tables:
        raise SystemExit("Restore a verified Control backup before running this check")
    with engine.connect() as db:
        before = {name: db.scalar(text(f'SELECT count(*) FROM "{name}"'))
                  for name in tables if re.fullmatch(r"[a-z_]+", name) and name != "alembic_version"}
    # This process is dedicated to migration validation; production settings
    # and its database are never modified by the command.
    os.environ["HERMES_CONTROL_DATABASE_URL"] = url.render_as_string(hide_password=False)
    get_settings.cache_clear()
    config = Config(str(alembic_config_path()))
    command.upgrade(config, "head")
    with engine.connect() as db:
        after = {name: db.scalar(text(f'SELECT count(*) FROM "{name}"')) for name in before}
    engine.dispose()
    if before != after:
        raise SystemExit("Migration changed existing row counts; review before deployment")
    print("Restored backup migrated successfully; existing table row counts preserved.")


if __name__ == "__main__":
    main()
