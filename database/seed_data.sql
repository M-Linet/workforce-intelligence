-- Fill dw.dim_date with one row per day, 2008-01-01 to 2027-12-31.
-- Safe to re-run: existing dates are skipped (ON CONFLICT DO NOTHING).
INSERT INTO dw.dim_date (date_key, full_date, year, quarter, month, month_name,
                         year_month, month_start, day_of_week, is_month_end)
SELECT
    TO_CHAR(d, 'YYYYMMDD')::INTEGER,
    d::DATE,
    EXTRACT(YEAR FROM d),
    EXTRACT(QUARTER FROM d),
    EXTRACT(MONTH FROM d),
    TO_CHAR(d, 'FMMonth'),
    TO_CHAR(d, 'YYYY-MM'),
    DATE_TRUNC('month', d)::DATE,
    EXTRACT(ISODOW FROM d),
    d::DATE = (DATE_TRUNC('month', d) + INTERVAL '1 month - 1 day')::DATE
FROM generate_series(DATE '2008-01-01', DATE '2027-12-31', INTERVAL '1 day') AS d
ON CONFLICT (date_key) DO NOTHING;