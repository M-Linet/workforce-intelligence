"""
Empty the warehouse, staging and audit tables (DEVELOPMENT ONLY).

Use this to rebuild everything from scratch after changing pipeline rules:
    python -m src.database.reset --yes
    python -m src.database.run_sql database/seed_data.sql
    python -m src.pipeline

Table structures are kept; only data is removed. The calendar (dim_date) is
also kept, so re-seeding is optional.
"""

import sys

from sqlalchemy import text

from src.database.connection import get_engine

TABLES = [
    "dw.fact_workforce_snapshot", "dw.fact_employee_movement", "dw.fact_recruitment",
    "dw.dim_employee", "dw.dim_department", "dw.dim_location",
    "staging.employee_master", "staging.workforce_snapshot", "staging.movements",
    "staging.recruitment", "staging.departments", "staging.offices",
    "audit.rejected_records", "audit.data_quality_issues", "audit.etl_audit_log",
    "audit.etl_file_registry", "audit.etl_run",
]

if __name__ == "__main__":
    if "--yes" not in sys.argv:
        sys.exit("This deletes ALL loaded data. Re-run with --yes to confirm.")
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    print(f"Emptied {len(TABLES)} tables. dim_date kept. Run the pipeline to reload.")
