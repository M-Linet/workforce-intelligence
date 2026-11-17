"""
Database connection module.

Reads connection settings from the .env file and returns a SQLAlchemy engine.
Every part of the project that needs the database imports get_engine() from here,
so connection logic lives in exactly one place.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine

# Load variables from .env into the environment
load_dotenv()

REQUIRED_VARS = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]


def get_engine() -> Engine:
    """Build a SQLAlchemy engine from environment variables."""
    missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
    if missing:
        raise EnvironmentError(
            f"Missing database settings in .env: {', '.join(missing)}"
        )

    # URL.create safely handles special characters (@, #, %) in passwords
    url = URL.create(
        drivername="postgresql+psycopg",
        username=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        database=os.getenv("DB_NAME"),
    )

    # pool_pre_ping checks the connection is alive before using it
    return create_engine(url, pool_pre_ping=True)


if __name__ == "__main__":
    # Quick self-test: run with  python -m src.database.connection
    engine = get_engine()
    with engine.connect() as conn:
        db = conn.execute(text("SELECT current_database()")).scalar()
        user = conn.execute(text("SELECT current_user")).scalar()
        version = conn.execute(text("SHOW server_version")).scalar()
    print(f"Connected to database : {db}")
    print(f"Connected as user     : {user}")
    print(f"PostgreSQL version    : {version}")
    print("CONNECTION OK")