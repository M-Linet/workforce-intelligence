-- =====================================================================
-- Reporting views (dw). Power BI and the reporting module read these.
-- Safe to re-run: CREATE OR REPLACE.
-- Run with:  python -m src.database.run_sql database/views.sql
-- DISCLAIMER: all data is SYNTHETIC.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Employees with readable department and location (current state)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW dw.v_employee AS
SELECT e.employee_id, e.first_name, e.last_name, e.gender, e.date_of_birth,
       DATE_PART('year', AGE(COALESCE(e.exit_date, CURRENT_DATE), e.date_of_birth))::INT AS age,
       e.nationality, e.employment_type, e.grade_level,
       CASE WHEN e.grade_level LIKE 'G%' THEN 'General Service'
            WHEN e.grade_level LIKE 'P%' THEN 'Professional'
            WHEN e.grade_level LIKE 'D%' THEN 'Director'
            ELSE 'Non-staff' END                                    AS grade_category,
       e.job_title,
       COALESCE(d.department_name, 'Unassigned')                   AS department,
       d.directorate,
       l.office_name, l.city AS duty_station, l.country AS country_of_assignment, l.region,
       e.supervisor_id, e.hire_date, e.contract_start_date, e.contract_end_date,
       e.employment_status, e.exit_date,
       ROUND((COALESCE(e.exit_date, CURRENT_DATE) - e.hire_date) / 365.25, 1) AS years_of_service,
       (e.nationality = l.country)                                  AS is_national_of_duty_country
FROM dw.dim_employee e
LEFT JOIN dw.dim_department d ON d.department_key = e.current_department_key
JOIN dw.dim_location l        ON l.location_key  = e.current_location_key;

-- ---------------------------------------------------------------------
-- Monthly headcount by office, department, grade (from snapshots)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW dw.v_headcount_monthly AS
SELECT s.reporting_month, dt.full_date AS month_end_date,
       l.office_id, l.office_name, l.country, l.region,
       COALESCE(d.department_name, 'Unassigned') AS department,
       s.grade_level, e.gender, e.employment_type,
       SUM(s.headcount)                                    AS headcount,
       COUNT(*) FILTER (WHERE s.employment_status = 'Separated') AS separated_in_month
FROM dw.fact_workforce_snapshot s
JOIN dw.dim_date dt           ON dt.date_key = s.month_end_date_key
JOIN dw.dim_location l        ON l.location_key = s.location_key
LEFT JOIN dw.dim_department d ON d.department_key = s.department_key
JOIN dw.dim_employee e        ON e.employee_key = s.employee_key
GROUP BY s.reporting_month, dt.full_date, l.office_id, l.office_name, l.country, l.region,
         d.department_name, s.grade_level, e.gender, e.employment_type;

-- ---------------------------------------------------------------------
-- Monthly hires, exits and turnover (organisation level)
--   Headcount-based turnover = exits in month / average headcount
--   Voluntary attrition      = resignations only / average headcount
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW dw.v_turnover_monthly AS
WITH months AS (
    SELECT DISTINCT reporting_month FROM dw.fact_workforce_snapshot
),
headcount AS (
    SELECT reporting_month, SUM(headcount) AS closing_headcount
    FROM dw.fact_workforce_snapshot GROUP BY reporting_month
),
exits AS (
    SELECT TO_CHAR(effective_date, 'YYYY-MM') AS reporting_month,
           COUNT(*) FILTER (WHERE is_exit)                                 AS exits,
           COUNT(*) FILTER (WHERE is_voluntary_exit)                       AS resignations,
           COUNT(*) FILTER (WHERE movement_type = 'Contract expiry')       AS contract_expiries,
           COUNT(*) FILTER (WHERE movement_type = 'Retirement')            AS retirements,
           COUNT(*) FILTER (WHERE movement_type = 'Termination')           AS terminations,
           COUNT(*) FILTER (WHERE movement_type = 'Promotion')             AS promotions,
           COUNT(*) FILTER (WHERE movement_type = 'Transfer')              AS transfers,
           COUNT(*) FILTER (WHERE movement_type = 'Contract renewal')      AS renewals
    FROM dw.fact_employee_movement GROUP BY 1
),
hires AS (
    SELECT TO_CHAR(hire_date, 'YYYY-MM') AS reporting_month, COUNT(*) AS hires
    FROM dw.dim_employee GROUP BY 1
)
SELECT m.reporting_month,
       LAG(h.closing_headcount) OVER (ORDER BY m.reporting_month) AS opening_headcount,
       h.closing_headcount,
       COALESCE(hi.hires, 0) AS hires,
       COALESCE(x.exits, 0) AS exits, COALESCE(x.resignations, 0) AS resignations,
       COALESCE(x.contract_expiries, 0) AS contract_expiries, COALESCE(x.retirements, 0) AS retirements,
       COALESCE(x.terminations, 0) AS terminations, COALESCE(x.promotions, 0) AS promotions,
       COALESCE(x.transfers, 0) AS transfers, COALESCE(x.renewals, 0) AS renewals,
       ROUND(100.0 * COALESCE(x.exits, 0) /
             NULLIF((COALESCE(LAG(h.closing_headcount) OVER (ORDER BY m.reporting_month), h.closing_headcount)
                     + h.closing_headcount) / 2.0, 0), 2) AS turnover_rate_pct,
       ROUND(100.0 * COALESCE(x.resignations, 0) /
             NULLIF((COALESCE(LAG(h.closing_headcount) OVER (ORDER BY m.reporting_month), h.closing_headcount)
                     + h.closing_headcount) / 2.0, 0), 2) AS voluntary_attrition_pct
FROM months m
JOIN headcount h     ON h.reporting_month = m.reporting_month
LEFT JOIN exits x    ON x.reporting_month = m.reporting_month
LEFT JOIN hires hi   ON hi.reporting_month = m.reporting_month;

-- ---------------------------------------------------------------------
-- Recruitment with readable dimensions and funnel conversion rates
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW dw.v_recruitment AS
SELECT r.recruitment_id, r.vacancy_id, r.job_title,
       COALESCE(d.department_name, 'Unassigned') AS department, l.office_name, l.country, l.region,
       r.vacancy_open_date, r.vacancy_close_date,
       TO_CHAR(COALESCE(r.vacancy_close_date, r.vacancy_open_date), 'YYYY-MM') AS reporting_month,
       r.recruitment_status, r.application_count, r.shortlisted_count, r.interviewed_count,
       r.offers_made, r.candidates_hired,
       CASE WHEN r.recruitment_status = 'Filled' THEN r.days_to_fill END AS days_to_fill,
       ROUND(100.0 * r.shortlisted_count / NULLIF(r.application_count, 0), 1) AS shortlist_rate_pct,
       ROUND(100.0 * r.candidates_hired / NULLIF(r.offers_made, 0), 1)       AS offer_acceptance_pct
FROM dw.fact_recruitment r
LEFT JOIN dw.dim_department d ON d.department_key = r.department_key
JOIN dw.dim_location l        ON l.location_key = r.location_key;

-- ---------------------------------------------------------------------
-- Data-quality scores per office, dataset and month
--   For each of the 5 dimensions: share of records with NO issue in it.
--   overall_score = average of the 5 dimension scores.
--   Only record-level issues count here; file-level problems are
--   reported separately in v_file_quality.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW dw.v_dq_scores AS
WITH files AS (
    SELECT file_id, office_id, dataset, reporting_month, row_count
    FROM audit.etl_file_registry
    WHERE status = 'processed' AND row_count > 0 AND office_id <> 'HQ'
),
affected AS (
    SELECT file_id,
           COUNT(DISTINCT source_row) FILTER (WHERE dimension = 'completeness')          AS completeness,
           COUNT(DISTINCT source_row) FILTER (WHERE dimension = 'uniqueness')            AS uniqueness,
           COUNT(DISTINCT source_row) FILTER (WHERE dimension = 'validity')              AS validity,
           COUNT(DISTINCT source_row) FILTER (WHERE dimension = 'consistency')           AS consistency,
           COUNT(DISTINCT source_row) FILTER (WHERE dimension = 'referential_integrity') AS referential_integrity,
           COUNT(DISTINCT source_row)                                                    AS any_issue
    FROM audit.data_quality_issues
    WHERE source_row IS NOT NULL
    GROUP BY file_id
),
per_group AS (
    SELECT f.office_id, f.dataset, f.reporting_month,
           SUM(f.row_count) AS records,
           SUM(COALESCE(a.completeness, 0)) AS c, SUM(COALESCE(a.uniqueness, 0)) AS u,
           SUM(COALESCE(a.validity, 0)) AS v, SUM(COALESCE(a.consistency, 0)) AS s,
           SUM(COALESCE(a.referential_integrity, 0)) AS r, SUM(COALESCE(a.any_issue, 0)) AS any_issue
    FROM files f LEFT JOIN affected a ON a.file_id = f.file_id
    GROUP BY f.office_id, f.dataset, f.reporting_month
)
SELECT office_id, dataset, reporting_month, records,
       records - any_issue                        AS records_without_issues,
       ROUND(100.0 * (1 - c::NUMERIC / records), 2) AS completeness_score,
       ROUND(100.0 * (1 - u::NUMERIC / records), 2) AS uniqueness_score,
       ROUND(100.0 * (1 - v::NUMERIC / records), 2) AS validity_score,
       ROUND(100.0 * (1 - s::NUMERIC / records), 2) AS consistency_score,
       ROUND(100.0 * (1 - r::NUMERIC / records), 2) AS referential_integrity_score,
       ROUND(100.0 * (5 - (c + u + v + s + r)::NUMERIC / records) / 5, 2) AS overall_score
FROM per_group;

-- ---------------------------------------------------------------------
-- File-level quality: every file received and what happened to it
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW dw.v_file_quality AS
SELECT r.office_id, r.dataset, r.reporting_month, r.file_name, r.status, r.row_count,
       r.file_size_bytes, r.received_at, r.processed_at, r.error_message
FROM audit.etl_file_registry r
WHERE r.status <> 'failed';      -- 'failed' rows are attempts superseded by a later run

-- ---------------------------------------------------------------------
-- Data-quality issues with rule descriptions, for drill-down
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW dw.v_dq_issues AS
SELECT q.issue_id, q.run_id, q.office_id, q.dataset, r.reporting_month, r.file_name, q.source_row,
       q.record_key, q.column_name, q.rule_id, q.dimension, q.severity, q.action_taken,
       q.original_value, q.corrected_value, q.detected_at
FROM audit.data_quality_issues q
LEFT JOIN audit.etl_file_registry r ON r.file_id = q.file_id;
