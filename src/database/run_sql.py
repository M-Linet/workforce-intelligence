"""
Run one or more .sql files against the project database.

Usage:
    python -m src.database.run_sql database/schema.sql
    python -m src.database.run_sql database/schema.sql database/views.sql

Each file runs inside ONE transaction: if any statement fails, nothing from
that file is applied. The database is never left half-built.
"""

import sys
from pathlib import Path

from sqlalchemy import text

from src.config import PROJECT_ROOT
from src.database.connection import get_engine


def run_sql_file(path, engine=None) -> None:
    path = Path(path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    sql = path.read_text(encoding="utf-8")
    engine = engine or get_engine()
    with engine.begin() as conn:          # begin() = commit on success, roll back on error
        # Hand the file straight to the driver's cursor. Unlike exec_driver_sql, a plain
        # cursor.execute() with no parameters does not treat '%' (as in LIKE 'G%') as a
        # placeholder. It still runs inside the same transaction.
        conn.connection.cursor().execute(sql)
    print(f"OK  {path.relative_to(PROJECT_ROOT)}")


def list_objects(engine=None) -> None:
    """Print the project's schemas and the tables/views inside them."""
    engine = engine or get_engine()
    query = text("""
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        WHERE table_schema IN ('staging', 'dw', 'audit')
        ORDER BY table_schema, table_type, table_name
    """)
    with engine.connect() as conn:
        schemas = conn.execute(text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name IN ('staging', 'dw', 'audit') ORDER BY schema_name")).scalars().all()
        rows = conn.execute(query).all()
    print(f"\nSchemas: {', '.join(schemas) if schemas else '(none)'}")
    print(f"Tables and views: {len(rows)}")
    for schema, name, kind in rows:
        print(f"  {schema}.{name:<32} {'VIEW' if kind == 'VIEW' else 'TABLE'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m src.database.run_sql <file.sql> [more files...]")
    engine = get_engine()
    for f in sys.argv[1:]:
        run_sql_file(f, engine)
    list_objects(engine)
