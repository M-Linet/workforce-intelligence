-- =====================================================================
-- Workforce Intelligence & HR Analytics System — database schema
-- DISCLAIMER: all data loaded into this database is SYNTHETIC.
--
-- Safe to re-run: every object uses IF NOT EXISTS.
-- Run with:  python -m src.database.run_sql database/schema.sql
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. SCHEMAS
--    staging : data as extracted from office files, before validation
--    dw      : validated data in a star schema, used for reporting
--    audit   : what the pipeline did (files, runs, data-quality issues)
-- ---------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS dw;
CREATE SCHEMA IF NOT EXISTS audit;

COMMENT ON SCHEMA staging IS 'Landing zone: office submissions as extracted, before validation';
COMMENT ON SCHEMA dw      IS 'Data warehouse: validated data in a star schema for reporting';
COMMENT ON SCHEMA audit   IS 'Pipeline audit trail: file registry, run log, data-quality issues';