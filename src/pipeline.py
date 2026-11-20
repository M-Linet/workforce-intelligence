"""
Run the complete ETL pipeline:  EXTRACT -> TRANSFORM/VALIDATE -> LOAD

Usage:
    python -m src.pipeline                      # normal run
    python -m src.pipeline --triggered-by scheduler

Safe to run again at any time (idempotent):
  - files already processed are skipped (same path and SHA-256 hash)
  - re-sent files with identical content are recorded as duplicates, not reloaded
  - every warehouse insert is an upsert on the record's business key
  - the load is one transaction: a failure leaves the warehouse untouched

Every run is recorded in audit.etl_run and audit.etl_audit_log, and records
held for review are also exported to data/rejected/ as CSV.
"""

import argparse
import sys
import time

import pandas as pd

from src.config import REJECTED_DIR
from src.database.audit import finish_run, log_event, start_run
from src.database.connection import get_engine
from src.database.load import load_all
from src.ingestion.extract import run_extract
from src.logging_config import get_logger
from src.validation.validate import Validator

log = get_logger("pipeline")


def export_held(results: dict, run_id: int) -> None:
    held = [{"dataset": h["dataset"], "record_key": h["record_key"], "category": h["category"],
             "reasons": ";".join(h["reasons"]), "file_id": h["file_id"], **h["raw_record"]}
            for r in results.values() for h in r.held]
    if held:
        REJECTED_DIR.mkdir(parents=True, exist_ok=True)
        path = REJECTED_DIR / f"run_{run_id:04d}_held_records.csv"
        pd.DataFrame(held).to_csv(path, index=False, encoding="utf-8")
        log.info(f"Held records exported for review: {path}")


def run(triggered_by: str = "manual") -> int:
    engine = get_engine()
    run_id = start_run(engine, triggered_by)
    started = time.time()
    log.info(f"=== Pipeline run {run_id} started ({triggered_by}) ===")
    try:
        # 1. EXTRACT
        ex = run_extract(engine, run_id)

        # 2. TRANSFORM + VALIDATE
        results = Validator(engine).run()
        for name, r in results.items():
            msg = f"{name}: " + (", ".join(f"{k} {v}" for k, v in sorted(r.categories.items()))
                                 or "nothing new")
            log.info(msg)
            log_event(engine, run_id, "validate", msg, rows_affected=sum(r.categories.values()))

        # 3. LOAD (one transaction)
        with engine.begin() as conn:
            loaded = load_all(conn, run_id, results)
        log_event(engine, run_id, "load", f"Loaded: {loaded}", rows_affected=sum(
            v for k, v in loaded.items() if k not in ("issues", "held")))
        export_held(results, run_id)

        n_loaded = sum(len(r.loadable) for r in results.values())
        n_held = sum(len(r.held) for r in results.values())
        finish_run(engine, run_id, "success", files_found=ex["files_found"],
                   files_processed=ex.get("received", 0), records_loaded=n_loaded, records_rejected=n_held)
        log.info(f"=== Run {run_id} succeeded in {time.time() - started:.0f}s: "
                 f"{ex.get('received', 0)} new files, {n_loaded} records loaded, {n_held} held for review ===")
        return run_id

    except Exception as exc:
        log.exception(f"Run {run_id} FAILED: {exc}")
        finish_run(engine, run_id, "failed", error=f"{type(exc).__name__}: {exc}"[:1000])
        try:
            log_event(engine, run_id, "load", f"Run failed: {exc}"[:1000], level="ERROR")
        except Exception:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Workforce Intelligence ETL pipeline")
    parser.add_argument("--triggered-by", default="manual", choices=["manual", "scheduler"])
    args = parser.parse_args()
    try:
        run(args.triggered_by)
    except Exception:
        sys.exit(1)          # non-zero exit code: lets a scheduler see the failure


if __name__ == "__main__":
    main()
