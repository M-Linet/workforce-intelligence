-- =====================================================================
-- Workforce analysis queries (brief items 1-4, 7, 8)
-- Run any query in psql, DBeaver, or with:
--   python -m src.analytics.run_queries sql/workforce_analysis.sql
-- DISCLAIMER: all data is SYNTHETIC.
-- =====================================================================

-- Q1. Total active workforce by country (current state)
--     Techniques: JOIN, GROUP BY, window function for share of total
SELECT l.country,
       l.region,
       COUNT(*)                                                   AS active_employees,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1)         AS pct_of_workforce
FROM dw.dim_employee e
JOIN dw.dim_location l ON l.location_key = e.current_location_key
WHERE e.employment_status = 'Active'
GROUP BY l.country, l.region
ORDER BY active_employees DESC;

-- Q2. Active workforce by department and directorate, with subtotals
--     Techniques: LEFT JOIN (keeps employees with no department), ROLLUP subtotals,
--     GROUPING() to tell a subtotal row apart from a genuinely missing department
SELECT CASE WHEN GROUPING(d.directorate) = 1 THEN 'ALL DIRECTORATES'
            ELSE COALESCE(d.directorate, 'Unassigned') END                AS directorate,
       CASE WHEN GROUPING(d.department_name) = 1 THEN '-- subtotal --'
            ELSE COALESCE(d.department_name, 'Unassigned') END            AS department,
       COUNT(*)                                                           AS active_employees
FROM dw.dim_employee e
LEFT JOIN dw.dim_department d ON d.department_key = e.current_department_key
WHERE e.employment_status = 'Active'
GROUP BY ROLLUP (d.directorate, d.department_name)
ORDER BY GROUPING(d.directorate), d.directorate NULLS LAST,
         GROUPING(d.department_name), d.department_name NULLS LAST;

-- Q3. Gender distribution overall and by grade category
--     Techniques: CASE to group grades, FILTER for conditional counts
SELECT COALESCE(CASE WHEN e.grade_level LIKE 'G%' THEN '1. General Service'
                     WHEN e.grade_level LIKE 'P%' THEN '2. Professional'
                     WHEN e.grade_level LIKE 'D%' THEN '3. Director'
                     ELSE '4. Non-staff' END, '5. TOTAL')             AS grade_category,
       COUNT(*)                                                       AS active_employees,
       COUNT(*) FILTER (WHERE e.gender = 'Female')                    AS female,
       COUNT(*) FILTER (WHERE e.gender = 'Male')                      AS male,
       COUNT(*) FILTER (WHERE e.gender = 'Non-binary / Not disclosed') AS not_disclosed,
       ROUND(100.0 * COUNT(*) FILTER (WHERE e.gender = 'Female') / COUNT(*), 1) AS female_pct
FROM dw.dim_employee e
WHERE e.employment_status = 'Active'
GROUP BY ROLLUP (CASE WHEN e.grade_level LIKE 'G%' THEN '1. General Service'
                      WHEN e.grade_level LIKE 'P%' THEN '2. Professional'
                      WHEN e.grade_level LIKE 'D%' THEN '3. Director'
                      ELSE '4. Non-staff' END)
ORDER BY 1;

-- Q4. Contract (employment) type distribution by region
--     Techniques: JOIN, window function partitioned by region
SELECT l.region,
       e.employment_type,
       COUNT(*)                                                                AS active_employees,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY l.region), 1) AS pct_within_region
FROM dw.dim_employee e
JOIN dw.dim_location l ON l.location_key = e.current_location_key
WHERE e.employment_status = 'Active'
GROUP BY l.region, e.employment_type
ORDER BY l.region, active_employees DESC;

-- Q7. Monthly workforce trend with month-on-month change
--     Techniques: CTE, LAG window function
WITH monthly AS (
    SELECT reporting_month, SUM(headcount) AS headcount
    FROM dw.fact_workforce_snapshot
    GROUP BY reporting_month
)
SELECT reporting_month,
       headcount,
       headcount - LAG(headcount) OVER (ORDER BY reporting_month)                 AS change_vs_prior_month,
       ROUND(100.0 * (headcount - LAG(headcount) OVER (ORDER BY reporting_month))
             / LAG(headcount) OVER (ORDER BY reporting_month), 2)                  AS pct_change
FROM monthly
ORDER BY reporting_month;

-- Q8. Employee turnover: annual rates, total vs voluntary
--     Headcount-based turnover = exits / average month-end headcount.
--     Voluntary attrition counts resignations only. Contract expiries are
--     planned endings of temporary, consultant and intern contracts.
--     Techniques: CTEs, conditional aggregation, CASE
WITH exits AS (
    SELECT EXTRACT(YEAR FROM effective_date)::INT                       AS yr,
           COUNT(*) FILTER (WHERE is_exit)                              AS all_exits,
           COUNT(*) FILTER (WHERE movement_type = 'Resignation')        AS resignations,
           COUNT(*) FILTER (WHERE movement_type = 'Contract expiry')    AS contract_expiries,
           COUNT(*) FILTER (WHERE movement_type = 'Retirement')         AS retirements,
           COUNT(*) FILTER (WHERE movement_type = 'Termination')        AS terminations
    FROM dw.fact_employee_movement
    GROUP BY 1
),
avg_headcount AS (
    SELECT LEFT(reporting_month, 4)::INT AS yr,
           AVG(hc)                       AS avg_headcount,
           COUNT(*)                      AS months_covered
    FROM (SELECT reporting_month, SUM(headcount) AS hc
          FROM dw.fact_workforce_snapshot GROUP BY reporting_month) m
    GROUP BY 1
)
SELECT h.yr AS year, h.months_covered, ROUND(h.avg_headcount) AS avg_headcount,
       x.all_exits, x.resignations, x.contract_expiries, x.retirements, x.terminations,
       ROUND(100.0 * x.all_exits    / h.avg_headcount, 1) AS turnover_rate_pct,
       ROUND(100.0 * x.resignations / h.avg_headcount, 1) AS voluntary_attrition_pct,
       CASE WHEN h.months_covered < 12 THEN 'Partial year: rates not annualised' END AS note
FROM avg_headcount h
JOIN exits x ON x.yr = h.yr
ORDER BY h.yr;

-- Q8b. Turnover by office, ranked (whole period)
--      Techniques: CTE, RANK window function
WITH office_exits AS (
    SELECT l.office_name,
           COUNT(*) FILTER (WHERE m.is_exit)           AS exits,
           COUNT(*) FILTER (WHERE m.is_voluntary_exit) AS resignations
    FROM dw.fact_employee_movement m
    JOIN dw.dim_location l ON l.location_key = m.new_location_key
    GROUP BY l.office_name
),
office_hc AS (
    SELECT l.office_name, AVG(hc) AS avg_headcount
    FROM (SELECT location_key, reporting_month, SUM(headcount) AS hc
          FROM dw.fact_workforce_snapshot GROUP BY 1, 2) s
    JOIN dw.dim_location l ON l.location_key = s.location_key
    GROUP BY l.office_name
)
SELECT o.office_name, ROUND(h.avg_headcount) AS avg_headcount, o.exits, o.resignations,
       ROUND(100.0 * o.resignations / h.avg_headcount, 1)                         AS voluntary_attrition_pct,
       RANK() OVER (ORDER BY o.resignations / h.avg_headcount DESC)               AS attrition_rank
FROM office_exits o JOIN office_hc h USING (office_name)
ORDER BY attrition_rank;

-- Bonus. Reporting lines: the full management chain above one employee
--        Techniques: RECURSIVE CTE over the self-referencing supervisor_id
WITH RECURSIVE chain AS (
    SELECT employee_id, supervisor_id, job_title, 0 AS level
    FROM dw.dim_employee
    WHERE employee_id = (SELECT MIN(employee_id) FROM dw.dim_employee WHERE employment_status = 'Active')
    UNION ALL
    SELECT e.employee_id, e.supervisor_id, e.job_title, c.level + 1
    FROM dw.dim_employee e
    JOIN chain c ON e.employee_id = c.supervisor_id
)
SELECT level, employee_id, job_title FROM chain ORDER BY level;
