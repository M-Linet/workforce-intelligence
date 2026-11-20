"""
Run every query in a .sql analysis file and print (or save) the results.

Usage:
    python -m src.analytics.run_queries sql/workforce_analysis.sql
    python -m src.analytics.run_queries sql/*.sql --save      # also writes CSVs to reports/query_results/

A query's title is taken from the first comment line above it (e.g. '-- Q1. ...').
"""

import re
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from src.config import PROJECT_ROOT
from src.database.connection import get_engine

OUT_DIR = PROJECT_ROOT / "reports" / "query_results"


def split_queries(sql: str) -> list[tuple[str, str]]:
    """Split a file into (title, query) pairs on semicolons at line ends."""
    queries = []
    for chunk in re.split(r";\s*\n", sql):
        lines = chunk.strip().splitlines()
        titles = [l.strip("- ").strip() for l in lines if re.match(r"--\s*(Q\d+\w*|Bonus)\.", l.strip())]
        body = "\n".join(lines).strip()
        if re.search(r"^\s*(WITH|SELECT)\b", body, re.IGNORECASE | re.MULTILINE):
            queries.append((titles[0] if titles else "Query", body.rstrip(";")))
    return queries


def run_file(path: Path, engine, save: bool) -> None:
    for title, query in split_queries(path.read_text(encoding="utf-8")):
        with engine.connect() as conn:
            df = pd.read_sql(text(query), conn)
        print(f"\n{'=' * 90}\n{title}\n{'=' * 90}")
        print(df.to_string(index=False, max_rows=30))
        if save:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^0-9a-z]+", "_", title.lower()).strip("_")[:60]
            df.to_csv(OUT_DIR / f"{name}.csv", index=False)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("Usage: python -m src.analytics.run_queries <file.sql> [more.sql] [--save]")
    engine = get_engine()
    pd.set_option("display.width", 200)
    for f in args:
        p = Path(f) if Path(f).is_absolute() else PROJECT_ROOT / f
        run_file(p, engine, save="--save" in sys.argv)
