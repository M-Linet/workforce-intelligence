"""
Helpers for the audit schema: start and finish runs, write the step log.

Each helper commits on its own connection, so the audit trail survives
even when the main work of a run fails and is rolled back.
"""

from sqlalchemy import text
from sqlalchemy.engine import Engine


def start_run(engine: Engine, triggered_by: str = "manual") -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO audit.etl_run (triggered_by) VALUES (:t) RETURNING run_id"),
            {"t": triggered_by},
        ).scalar_one()


def finish_run(engine: Engine, run_id: int, status: str, error: str | None = None, **counts) -> None:
    """status is 'success' or 'failed'. counts may include files_found, files_processed,
    records_loaded, records_rejected."""
    sets = ", ".join(f"{k} = :{k}" for k in counts)
    sql = f"UPDATE audit.etl_run SET status = :status, finished_at = now(), error_message = :error" \
          f"{', ' + sets if sets else ''} WHERE run_id = :run_id"
    with engine.begin() as conn:
        conn.execute(text(sql), {"status": status, "error": error, "run_id": run_id, **counts})


def log_event(engine: Engine, run_id: int, stage: str, message: str,
              level: str = "INFO", rows_affected: int | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO audit.etl_audit_log (run_id, level, stage, message, rows_affected) "
                 "VALUES (:run_id, :level, :stage, :message, :rows)"),
            {"run_id": run_id, "level": level, "stage": stage, "message": message, "rows": rows_affected},
        )
