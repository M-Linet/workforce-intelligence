-- =====================================================================
-- Data-quality analysis queries (brief item 10 and dashboard page 5)
-- DISCLAIMER: all data is SYNTHETIC.
-- =====================================================================

-- Q10. Data-quality issues by source office, per dimension
--      Techniques: JOIN, FILTER, window function for rank
SELECT l.office_name,
       COUNT(*)                                                                   AS issues,
       COUNT(*) FILTER (WHERE q.dimension = 'completeness')                       AS completeness,
       COUNT(*) FILTER (WHERE q.dimension = 'uniqueness')                         AS uniqueness,
       COUNT(*) FILTER (WHERE q.dimension = 'validity')                           AS validity,
       COUNT(*) FILTER (WHERE q.dimension = 'consistency')                        AS consistency,
       COUNT(*) FILTER (WHERE q.dimension = 'referential_integrity')              AS referential_integrity,
       COUNT(*) FILTER (WHERE q.severity IN ('error', 'critical'))                AS serious,
       RANK() OVER (ORDER BY COUNT(*) DESC)                                       AS rank_most_issues
FROM audit.data_quality_issues q
JOIN dw.dim_location l ON l.office_id = q.office_id
GROUP BY l.office_name
ORDER BY issues DESC;

-- Q10b. Data-quality score per office (weighted by records), with grade
--       Techniques: aggregation over a view, CASE banding
SELECT office_id,
       SUM(records)                                                         AS records_received,
       ROUND(SUM(overall_score * records) / SUM(records), 2)                AS overall_score,
       ROUND(SUM(completeness_score * records) / SUM(records), 2)           AS completeness,
       ROUND(SUM(validity_score * records) / SUM(records), 2)               AS validity,
       ROUND(SUM(consistency_score * records) / SUM(records), 2)            AS consistency,
       ROUND(SUM(referential_integrity_score * records) / SUM(records), 2)  AS referential_integrity,
       CASE WHEN SUM(overall_score * records) / SUM(records) >= 99 THEN 'Good'
            WHEN SUM(overall_score * records) / SUM(records) >= 97.5 THEN 'Needs attention'
            ELSE 'Poor' END                                                 AS rating
FROM dw.v_dq_scores
GROUP BY office_id
ORDER BY overall_score;

-- Q10c. Is data quality improving? First 6 months vs last 6 months per office
--       Techniques: CTEs, conditional aggregation, CASE
WITH bounds AS (
    SELECT MIN(reporting_month) AS first_m, MAX(reporting_month) AS last_m FROM dw.v_dq_scores
),
periods AS (
    SELECT s.office_id, s.records, s.overall_score,
           CASE WHEN s.reporting_month <= TO_CHAR(TO_DATE(b.first_m, 'YYYY-MM') + INTERVAL '5 months', 'YYYY-MM')
                     THEN 'first_6'
                WHEN s.reporting_month >= TO_CHAR(TO_DATE(b.last_m, 'YYYY-MM') - INTERVAL '5 months', 'YYYY-MM')
                     THEN 'last_6' END AS period
    FROM dw.v_dq_scores s CROSS JOIN bounds b
)
SELECT office_id,
       ROUND(SUM(overall_score * records) FILTER (WHERE period = 'first_6')
             / SUM(records) FILTER (WHERE period = 'first_6'), 2)  AS score_first_6_months,
       ROUND(SUM(overall_score * records) FILTER (WHERE period = 'last_6')
             / SUM(records) FILTER (WHERE period = 'last_6'), 2)   AS score_last_6_months,
       ROUND(SUM(overall_score * records) FILTER (WHERE period = 'last_6')
             / SUM(records) FILTER (WHERE period = 'last_6')
           - SUM(overall_score * records) FILTER (WHERE period = 'first_6')
             / SUM(records) FILTER (WHERE period = 'first_6'), 2)  AS change_points
FROM periods
WHERE period IS NOT NULL
GROUP BY office_id
ORDER BY change_points DESC;

-- Q10d. Most frequent problems: which rules fire most, and what happens to the records
SELECT rule_id, dimension, severity, action_taken, COUNT(*) AS occurrences,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct_of_all_issues
FROM audit.data_quality_issues
GROUP BY rule_id, dimension, severity, action_taken
ORDER BY occurrences DESC;

-- Q10e. Record outcomes by dataset: valid / corrected / held
--       Techniques: UNION ALL of two sources, conditional aggregation
WITH staged AS (
    SELECT dataset, SUM(row_count) AS records
    FROM audit.etl_file_registry
    WHERE status = 'processed'
    GROUP BY dataset
),
held AS (
    SELECT dataset,
           COUNT(*) FILTER (WHERE category = 'manual_review') AS manual_review,
           COUNT(*) FILTER (WHERE category = 'rejected')      AS rejected
    FROM audit.rejected_records
    GROUP BY dataset
)
SELECT s.dataset, s.records AS records_received,
       COALESCE(h.manual_review, 0) AS manual_review,
       COALESCE(h.rejected, 0)      AS rejected,
       s.records - COALESCE(h.manual_review, 0) - COALESCE(h.rejected, 0) AS loaded,
       ROUND(100.0 * (s.records - COALESCE(h.manual_review, 0) - COALESCE(h.rejected, 0)) / s.records, 2) AS pct_loaded
FROM staged s LEFT JOIN held h USING (dataset)
ORDER BY s.dataset;

-- Q10f. Files that could not be used
SELECT office_id, dataset, reporting_month, file_name, status, error_message
FROM dw.v_file_quality
WHERE status IN ('unreadable', 'duplicate') OR (status = 'empty' AND file_size_bytes = 0)
ORDER BY office_id, reporting_month;
