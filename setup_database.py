"""Interactive database setup utility for the Leader-board project.

This script asks for DATABASE_URL, optionally stores it in .env,
and initializes the database using schema.sql.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
ENV_PATH = BASE_DIR / ".env"


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def prompt_database_url() -> str:
    load_dotenv()
    current = os.getenv("DATABASE_URL", "").strip()

    if current:
        print("Current DATABASE_URL found in environment.")
    entered = input("Enter DATABASE_URL (press Enter to use current value): ").strip()

    database_url = normalize_database_url(entered or current)
    if not database_url:
        raise ValueError("DATABASE_URL is required.")

    return database_url


def should_save_env() -> bool:
    choice = input("Save DATABASE_URL to .env? [Y/n]: ").strip().lower()
    return choice in ("", "y", "yes")


def upsert_database_url_in_env(database_url: str) -> None:
    formatted = database_url.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'DATABASE_URL="{formatted}"'

    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    replaced = False
    updated_lines: list[str] = []
    for line in lines:
        if line.strip().startswith("DATABASE_URL="):
            updated_lines.append(new_line)
            replaced = True
        else:
            updated_lines.append(line)

    if not replaced:
        updated_lines.append(new_line)

    ENV_PATH.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")


def load_schema_sql() -> str:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found: {SCHEMA_PATH}")

    raw_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    lines = []
    for line in raw_sql.splitlines():
        statement = line.strip().upper()
        if statement in {"BEGIN;", "COMMIT;"}:
            continue
        lines.append(line)
    return "\n".join(lines)


def is_schema_initialized(database_url: str) -> bool:
    engine = create_engine(database_url, future=True)
    with engine.connect() as connection:
        result = connection.execute(text("SELECT to_regclass('public.users') IS NOT NULL"))
        return bool(result.scalar())


def initialize_database(database_url: str, schema_sql: str) -> None:
    engine = create_engine(database_url, future=True)
    with engine.connect() as connection:
        with connection.begin():
            connection.exec_driver_sql(schema_sql)


def main() -> int:
    print("=== Leader-board Database Setup ===")

    try:
        database_url = prompt_database_url()

        if should_save_env():
            upsert_database_url_in_env(database_url)
            print("Saved DATABASE_URL to .env")

        if is_schema_initialized(database_url):
            print("Database appears initialized already (users table exists).")
            print("No changes were applied.")
            return 0

        schema_sql = load_schema_sql()
        initialize_database(database_url, schema_sql)

        print("Database setup complete.")
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}")
        return 1
    except SQLAlchemyError as exc:
        print("Database setup failed.")
        print(f"Details: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
