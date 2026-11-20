"""
LOAD stage: write validated records into the data warehouse.

Everything happens in ONE database transaction:
  dimensions -> facts -> data-quality issues -> held records -> file statuses
If any step fails, the whole load is rolled back and the warehouse is exactly
as it was before the run. Nothing is ever half-loaded.

Idempotency: every insert is an UPSERT on the record's business key
(INSERT ... ON CONFLICT ... DO UPDATE), so re-loading the same record
updates it instead of creating a duplicate.
"""

import calendar
import json

import pandas as pd
from sqlalchemy import text

EMP_KEY = "(SELECT employee_key FROM dw.dim_employee WHERE employee_id = :employee_id)"
DEPT_KEY = "(SELECT department_key FROM dw.dim_department WHERE department_id = :{col})"
LOC_KEY = "(SELECT location_key FROM dw.dim_location WHERE office_id = :{col})"


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list of dicts with real None instead of NaN/NaT."""
    if df is None or df.empty:
        return []
    return df.astype(object).where(df.notna(), None).to_dict("records")


def date_key(d) -> int | None:
    return None if d is None else int(d.strftime("%Y%m%d"))


def month_end_key(month: str) -> int:
    y, m = map(int, month.split("-"))
    return y * 10000 + m * 100 + calendar.monthrange(y, m)[1]


def upsert(conn, sql: str, rows: list[dict]) -> int:
    if rows:
        conn.execute(text(sql), rows)
    return len(rows)


def load_all(conn, run_id: int, results: dict) -> dict:
    n = {}
    n["departments"] = upsert(conn, """
        INSERT INTO dw.dim_department (department_id, department_name, department_category, directorate)
        VALUES (:department_id, :department_name, :department_category, :directorate)
        ON CONFLICT (department_id) DO UPDATE SET
            department_name = EXCLUDED.department_name, department_category = EXCLUDED.department_category,
            directorate = EXCLUDED.directorate, loaded_at = now()""", records(results["departments"].loadable))

    n["offices"] = upsert(conn, """
        INSERT INTO dw.dim_location (office_id, office_name, city, country, region, office_type)
        VALUES (:office_id, :office_name, :city, :country, :region, :office_type)
        ON CONFLICT (office_id) DO UPDATE SET
            office_name = EXCLUDED.office_name, city = EXCLUDED.city, country = EXCLUDED.country,
            region = EXCLUDED.region, office_type = EXCLUDED.office_type, loaded_at = now()""",
        records(results["offices"].loadable))

    n["employee_master"] = upsert(conn, f"""
        INSERT INTO dw.dim_employee (employee_id, first_name, last_name, gender, date_of_birth, nationality,
            employment_type, grade_level, job_title, current_department_key, current_location_key,
            supervisor_id, hire_date, contract_start_date, contract_end_date, employment_status,
            exit_date, last_updated)
        VALUES (:employee_id, :first_name, :last_name, :gender, :date_of_birth, :nationality,
            :employment_type, :grade_level, :job_title, {DEPT_KEY.format(col='department_id')},
            {LOC_KEY.format(col='source_office')}, :supervisor_id, :hire_date, :contract_start_date,
            :contract_end_date, :employment_status, :exit_date, :last_updated)
        ON CONFLICT (employee_id) DO UPDATE SET
            first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name, gender = EXCLUDED.gender,
            date_of_birth = EXCLUDED.date_of_birth, nationality = EXCLUDED.nationality,
            employment_type = EXCLUDED.employment_type, grade_level = EXCLUDED.grade_level,
            job_title = EXCLUDED.job_title, current_department_key = EXCLUDED.current_department_key,
            current_location_key = EXCLUDED.current_location_key, supervisor_id = EXCLUDED.supervisor_id,
            hire_date = EXCLUDED.hire_date, contract_start_date = EXCLUDED.contract_start_date,
            contract_end_date = EXCLUDED.contract_end_date, employment_status = EXCLUDED.employment_status,
            exit_date = EXCLUDED.exit_date, last_updated = EXCLUDED.last_updated, loaded_at = now()""",
        records(results["employee_master"].loadable))

    snaps = records(results["workforce_snapshot"].loadable)
    for r in snaps:
        r["month_end_date_key"] = month_end_key(r["reporting_month"])
    n["workforce_snapshot"] = upsert(conn, f"""
        INSERT INTO dw.fact_workforce_snapshot (snapshot_id, employee_key, month_end_date_key, reporting_month,
            department_key, location_key, grade_level, employment_status, source_file_id)
        VALUES (:snapshot_id, {EMP_KEY}, :month_end_date_key, :reporting_month,
            {DEPT_KEY.format(col='department_id')}, {LOC_KEY.format(col='office_id')},
            :grade_level, :employment_status, :source_file_id)
        ON CONFLICT (employee_key, reporting_month) DO UPDATE SET
            snapshot_id = EXCLUDED.snapshot_id, department_key = EXCLUDED.department_key,
            location_key = EXCLUDED.location_key, grade_level = EXCLUDED.grade_level,
            employment_status = EXCLUDED.employment_status, source_file_id = EXCLUDED.source_file_id,
            loaded_at = now()""", snaps)

    moves = records(results["movements"].loadable)
    for r in moves:
        r["effective_date_key"] = date_key(r["effective_date"])
    n["movements"] = upsert(conn, f"""
        INSERT INTO dw.fact_employee_movement (movement_id, employee_key, effective_date_key, effective_date,
            movement_type, previous_department_key, new_department_key, previous_location_key,
            new_location_key, previous_grade, new_grade, source_file_id)
        VALUES (:movement_id, {EMP_KEY}, :effective_date_key, :effective_date, :movement_type,
            {DEPT_KEY.format(col='previous_department')}, {DEPT_KEY.format(col='new_department')},
            {LOC_KEY.format(col='previous_location')}, {LOC_KEY.format(col='new_location')},
            :previous_grade, :new_grade, :source_file_id)
        ON CONFLICT (movement_id) DO UPDATE SET
            employee_key = EXCLUDED.employee_key, effective_date_key = EXCLUDED.effective_date_key,
            effective_date = EXCLUDED.effective_date, movement_type = EXCLUDED.movement_type,
            previous_department_key = EXCLUDED.previous_department_key,
            new_department_key = EXCLUDED.new_department_key,
            previous_location_key = EXCLUDED.previous_location_key,
            new_location_key = EXCLUDED.new_location_key, previous_grade = EXCLUDED.previous_grade,
            new_grade = EXCLUDED.new_grade, source_file_id = EXCLUDED.source_file_id, loaded_at = now()""", moves)

    recs = records(results["recruitment"].loadable)
    for r in recs:
        r["open_date_key"] = date_key(r["vacancy_open_date"])
        r["close_date_key"] = date_key(r["vacancy_close_date"])
    n["recruitment"] = upsert(conn, f"""
        INSERT INTO dw.fact_recruitment (recruitment_id, vacancy_id, department_key, location_key, job_title,
            open_date_key, close_date_key, vacancy_open_date, vacancy_close_date, application_count,
            shortlisted_count, interviewed_count, offers_made, candidates_hired, recruitment_status,
            source_file_id)
        VALUES (:recruitment_id, :vacancy_id, {DEPT_KEY.format(col='department_id')},
            {LOC_KEY.format(col='office_id')}, :job_title, :open_date_key, :close_date_key,
            :vacancy_open_date, :vacancy_close_date, :application_count, :shortlisted_count,
            :interviewed_count, :offers_made, :candidates_hired, :recruitment_status, :source_file_id)
        ON CONFLICT (recruitment_id) DO UPDATE SET
            vacancy_id = EXCLUDED.vacancy_id, department_key = EXCLUDED.department_key,
            location_key = EXCLUDED.location_key, job_title = EXCLUDED.job_title,
            open_date_key = EXCLUDED.open_date_key, close_date_key = EXCLUDED.close_date_key,
            vacancy_open_date = EXCLUDED.vacancy_open_date, vacancy_close_date = EXCLUDED.vacancy_close_date,
            application_count = EXCLUDED.application_count, shortlisted_count = EXCLUDED.shortlisted_count,
            interviewed_count = EXCLUDED.interviewed_count, offers_made = EXCLUDED.offers_made,
            candidates_hired = EXCLUDED.candidates_hired, recruitment_status = EXCLUDED.recruitment_status,
            source_file_id = EXCLUDED.source_file_id, loaded_at = now()""", recs)

    # ---- Audit: issues and held records ------------------------------------------
    issues = [dict(i, run_id=run_id) for r in results.values() for i in r.issues]
    n["issues"] = upsert(conn, """
        INSERT INTO audit.data_quality_issues (run_id, file_id, office_id, dataset, record_key, column_name,
            rule_id, dimension, severity, action_taken, original_value, corrected_value)
        VALUES (:run_id, :file_id, :office_id, :dataset, :record_key, :column_name,
            :rule_id, :dimension, :severity, :action_taken, :original_value, :corrected_value)""", issues)

    held = [{"run_id": run_id, **h, "raw_record": json.dumps(h["raw_record"], default=str)}
            for r in results.values() for h in r.held]
    n["held"] = upsert(conn, """
        INSERT INTO audit.rejected_records (run_id, file_id, dataset, record_key, category, reasons, raw_record)
        VALUES (:run_id, :file_id, :dataset, :record_key, :category, :reasons, CAST(:raw_record AS JSONB))""", held)

    # File-level problems become DQ issues too. Not limited to this run: if an earlier
    # run failed after registering a bad file, its issue is recorded now instead.
    conn.execute(text("""
        INSERT INTO audit.data_quality_issues (run_id, file_id, office_id, dataset, rule_id, dimension,
                                               severity, action_taken, original_value)
        SELECT :run_id, r.file_id, r.office_id, COALESCE(r.dataset, 'unknown'),
               CASE r.status WHEN 'duplicate' THEN 'FILE_DUPLICATE'
                             WHEN 'unreadable' THEN 'FILE_UNREADABLE' ELSE 'FILE_EMPTY' END,
               CASE r.status WHEN 'duplicate' THEN 'uniqueness'
                             WHEN 'unreadable' THEN 'validity' ELSE 'completeness' END,
               CASE r.status WHEN 'duplicate' THEN 'info' ELSE 'critical' END,
               'rejected', r.file_name
        FROM audit.etl_file_registry r
        WHERE (r.status IN ('duplicate', 'unreadable') OR (r.status = 'empty' AND r.file_size_bytes = 0))
          AND NOT EXISTS (SELECT 1 FROM audit.data_quality_issues q
                          WHERE q.file_id = r.file_id AND q.rule_id LIKE 'FILE%')"""), {"run_id": run_id})

    # Every file staged in this run is now processed. (Header-only returns stay
    # 'empty': two of them can share a hash, and only one 'processed' row per hash is allowed.)
    conn.execute(text("""
        UPDATE audit.etl_file_registry SET status = 'processed', processed_at = now()
        WHERE run_id = :run_id AND status = 'received'"""), {"run_id": run_id})
    return n
