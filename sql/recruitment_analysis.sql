-- =====================================================================
-- Recruitment analysis queries (brief items 5, 6, 9)
-- DISCLAIMER: all data is SYNTHETIC.
-- =====================================================================

-- Q5. Recruitment success and funnel conversion, by year of closure
--     Success rate = filled / closed (cancelled excluded: withdrawn, not failed)
--     Techniques: CTE, FILTER, CASE
WITH closed AS (
    SELECT EXTRACT(YEAR FROM vacancy_close_date)::INT AS year, *
    FROM dw.fact_recruitment
    WHERE recruitment_status <> 'Open' AND vacancy_close_date IS NOT NULL
)
SELECT CASE WHEN GROUPING(year) = 1 THEN 'All years' ELSE year::TEXT END AS year,
       COUNT(*)                                                         AS processes_closed,
       COUNT(*) FILTER (WHERE recruitment_status = 'Filled')            AS filled,
       COUNT(*) FILTER (WHERE recruitment_status = 'Unsuccessful')      AS unsuccessful,
       COUNT(*) FILTER (WHERE recruitment_status = 'Cancelled')         AS cancelled,
       ROUND(100.0 * COUNT(*) FILTER (WHERE recruitment_status = 'Filled')
             / NULLIF(COUNT(*) FILTER (WHERE recruitment_status <> 'Cancelled'), 0), 1) AS success_rate_pct,
       ROUND(100.0 * SUM(shortlisted_count) / NULLIF(SUM(application_count), 0), 1)   AS shortlist_rate_pct,
       ROUND(100.0 * SUM(interviewed_count) / NULLIF(SUM(shortlisted_count), 0), 1)   AS interview_rate_pct,
       ROUND(100.0 * SUM(candidates_hired)  / NULLIF(SUM(offers_made), 0), 1)         AS offer_acceptance_pct
FROM closed
GROUP BY ROLLUP (year)
ORDER BY GROUPING(year), year;

-- Q5b. Recruitment funnel totals (feeds the funnel chart)
SELECT stage, candidates,
       ROUND(100.0 * candidates / FIRST_VALUE(candidates) OVER (ORDER BY step), 2) AS pct_of_applicants
FROM (
    SELECT 1 AS step, 'Applications' AS stage, SUM(application_count) AS candidates FROM dw.fact_recruitment
    UNION ALL SELECT 2, 'Shortlisted', SUM(shortlisted_count) FROM dw.fact_recruitment
    UNION ALL SELECT 3, 'Interviewed', SUM(interviewed_count) FROM dw.fact_recruitment
    UNION ALL SELECT 4, 'Offers',      SUM(offers_made)       FROM dw.fact_recruitment
    UNION ALL SELECT 5, 'Hired',       SUM(candidates_hired)  FROM dw.fact_recruitment
) f
ORDER BY step;

-- Q6. Average time-to-hire (days from opening to selection), by office
--     Only filled processes with both dates. Median shown too: less affected by outliers.
--     Techniques: JOIN, AVG, PERCENTILE_CONT, RANK
SELECT l.office_name,
       COUNT(*)                                                           AS filled_processes,
       ROUND(AVG(r.days_to_fill))                                         AS avg_days_to_hire,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r.days_to_fill)        AS median_days_to_hire,
       RANK() OVER (ORDER BY AVG(r.days_to_fill))                         AS fastest_rank
FROM dw.fact_recruitment r
JOIN dw.dim_location l ON l.location_key = r.location_key
WHERE r.recruitment_status = 'Filled' AND r.days_to_fill IS NOT NULL
GROUP BY l.office_name
ORDER BY avg_days_to_hire;

-- Q9. Department-level recruitment performance
--     Techniques: LEFT JOIN, CTE, CASE, window function comparing to the organisation average
WITH dept AS (
    SELECT COALESCE(d.department_name, 'Unassigned') AS department,
           COUNT(*)                                                         AS processes,
           COUNT(*) FILTER (WHERE r.recruitment_status = 'Filled')          AS filled,
           COUNT(*) FILTER (WHERE r.recruitment_status = 'Unsuccessful')    AS unsuccessful,
           ROUND(AVG(r.days_to_fill) FILTER (WHERE r.recruitment_status = 'Filled')) AS avg_days_to_hire,
           ROUND(AVG(r.application_count))                                  AS avg_applications
    FROM dw.fact_recruitment r
    LEFT JOIN dw.dim_department d ON d.department_key = r.department_key
    GROUP BY 1
)
SELECT department, processes, filled, unsuccessful, avg_days_to_hire, avg_applications,
       ROUND(100.0 * filled / NULLIF(filled + unsuccessful, 0), 1)          AS success_rate_pct,
       avg_days_to_hire - ROUND(AVG(avg_days_to_hire) OVER ())              AS days_vs_org_average,
       CASE WHEN avg_days_to_hire > 1.1 * AVG(avg_days_to_hire) OVER () THEN 'Slower than average'
            WHEN avg_days_to_hire < 0.9 * AVG(avg_days_to_hire) OVER () THEN 'Faster than average'
            ELSE 'Around average' END                                       AS speed_band
FROM dept
ORDER BY avg_days_to_hire DESC NULLS LAST;

-- Q9b. Posts that needed more than one advertisement round
--      Techniques: GROUP BY with HAVING, STRING_AGG
SELECT vacancy_id,
       COUNT(*)                                                         AS rounds,
       STRING_AGG(recruitment_status, ' -> ' ORDER BY vacancy_open_date) AS history,
       MIN(vacancy_open_date)                                           AS first_opened,
       MAX(vacancy_close_date)                                          AS finally_closed,
       MAX(vacancy_close_date) - MIN(vacancy_open_date)                 AS total_days
FROM dw.fact_recruitment
GROUP BY vacancy_id
HAVING COUNT(*) > 1
ORDER BY total_days DESC
LIMIT 15;
