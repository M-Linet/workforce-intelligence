"""
EXTRACT stage: find new office files, register them, and land their rows in staging.

For each file under data/raw/:
  1. Already handled in an earlier run (same path, same content)  -> skip
  2. 0 bytes (failed upload)                                       -> 'empty'
  3. Cannot be parsed, or columns not recognisable                 -> 'unreadable'
  4. Header only, no rows ("nothing to report")                    -> 'empty'
  5. Same CONTENT already received under another name              -> 'duplicate'
  6. Otherwise: standardise column names, insert rows into
     staging.<dataset> with file_id + source_row                   -> 'received'

Files marked 'received' become 'processed' only after the LOAD stage succeeds.
Original files in data/raw/ are never modified.

Run on its own:  python -m src.ingestion.extract
"""

from collections import Counter

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.config import load_yaml
from src.database.audit import log_event
from src.ingestion.files import SourceFile, discover
from src.ingestion.readers import UnreadableFileError, read_table, standardise_columns
from src.logging_config import get_logger

log = get_logger("extract")

STAGING_TABLES = ["employee_master", "workforce_snapshot", "movements", "recruitment", "departments", "offices"]
FINAL_STATUSES = ("processed", "duplicate", "empty", "unreadable")


def register(conn, run_id: int, f: SourceFile, status: str, row_count=None, error=None) -> int:
    return conn.execute(text("""
        INSERT INTO audit.etl_file_registry
            (run_id, file_path, file_name, office_id, dataset, reporting_month,
             file_hash, file_size_bytes, row_count, status, error_message)
        VALUES (:run_id, :path, :name, :office, :dataset, :month, :hash, :size, :rows, :status, :error)
        RETURNING file_id"""), {
        "run_id": run_id, "path": f.rel_path, "name": f.path.name, "office": f.office_id,
        "dataset": f.dataset, "month": f.reporting_month, "hash": f.sha256, "size": f.size_bytes,
        "rows": row_count, "status": status, "error": error,
    }).scalar_one()


def prepare_staging(engine: Engine) -> None:
    """Staging is a per-run scratch area: empty it, and mark files left 'received'
    by an interrupted earlier run as failed, so they are extracted again."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(f"staging.{t}" for t in STAGING_TABLES)))
        conn.execute(text("""UPDATE audit.etl_file_registry
                             SET status = 'failed', error_message = 'Interrupted run: re-extracted later'
                             WHERE status = 'received'"""))


def run_extract(engine: Engine, run_id: int) -> dict:
    cfg = load_yaml("pipeline.yaml")
    prepare_staging(engine)

    with engine.connect() as conn:
        known = {(r.file_path, r.file_hash) for r in conn.execute(text(
            "SELECT file_path, file_hash FROM audit.etl_file_registry "
            f"WHERE status IN {FINAL_STATUSES}"))}
        processed_hashes = set(conn.execute(text(
            "SELECT file_hash FROM audit.etl_file_registry WHERE status = 'processed'")).scalars())

    files = discover(cfg)
    outcome = Counter()
    seen_this_run = set()
    rows_staged = 0

    for f in files:
        if (f.rel_path, f.sha256) in known:
            outcome["already handled"] += 1
            continue

        with engine.begin() as conn:                 # one transaction per file
            if f.dataset is None:
                register(conn, run_id, f, "failed", error="File name not recognised")
                outcome["failed"] += 1
                log.warning(f"Unrecognised file name: {f.rel_path}")
                continue

            if f.size_bytes == 0:
                register(conn, run_id, f, "empty", row_count=0, error="0 bytes: no header, likely a failed upload")
                outcome["empty (0 bytes)"] += 1
                log.warning(f"Empty file (0 bytes): {f.rel_path}")
                continue

            try:
                raw = read_table(f.path)
                df, unknown, missing, share = standardise_columns(raw, f.dataset, cfg)
                if share < cfg["min_recognised_column_share"]:
                    raise UnreadableFileError(f"Only {share:.0%} of columns recognised: {list(raw.columns)[:5]}")
            except UnreadableFileError as exc:
                register(conn, run_id, f, "unreadable", error=str(exc)[:500])
                outcome["unreadable"] += 1
                log.warning(f"Unreadable file: {f.rel_path} ({str(exc)[:80]})")
                continue

            # A header with no rows is a legitimate "nothing to report" return.
            # Checked BEFORE duplicates: two such files are byte-identical, so their
            # hashes match even though they are different months' returns.
            if df.empty:
                register(conn, run_id, f, "empty", row_count=0, error="Header only: nothing to report")
                outcome["header only"] += 1
                continue

            if f.sha256 in processed_hashes or f.sha256 in seen_this_run:
                register(conn, run_id, f, "duplicate", row_count=len(df), error="Identical content already received")
                outcome["duplicate"] += 1
                log.warning(f"Duplicate content (re-sent file): {f.rel_path}")
                continue

            file_id = register(conn, run_id, f, "received", row_count=len(df))
            seen_this_run.add(f.sha256)
            if unknown or missing:
                log.info(f"{f.rel_path}: unknown columns {unknown}, missing columns {missing}")
            # source_row = line number in the original file (line 1 is the header)
            df.insert(0, "source_row", range(2, len(df) + 2))
            df.insert(0, "file_id", file_id)
            df = df.astype(object).where(df.notna(), None)
            df.to_sql(f.dataset, conn, schema="staging", if_exists="append",
                      index=False, method="multi", chunksize=1000)
            rows_staged += len(df)
            outcome["received"] += 1

    summary = {"files_found": len(files), "rows_staged": rows_staged, **outcome}
    log_event(engine, run_id, "extract",
              f"Files found: {len(files)}; " + ", ".join(f"{k}: {v}" for k, v in outcome.items()),
              rows_affected=rows_staged)
    log.info(f"Extract complete: {summary}")
    return summary


if __name__ == "__main__":
    from src.database.audit import finish_run, start_run
    from src.database.connection import get_engine

    engine = get_engine()
    run_id = start_run(engine)
    try:
        s = run_extract(engine, run_id)
        finish_run(engine, run_id, "success", files_found=s["files_found"])
    except Exception as exc:
        finish_run(engine, run_id, "failed", error=str(exc)[:1000])
        raise
