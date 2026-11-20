"""
Unit tests for the value-cleaning rules (src/transformation/standardise.py).
Run with:  pytest -v
"""

from datetime import date

from src.transformation import standardise as st

GENDER = st.build_lookup({"Female": ["f", "woman"], "Male": ["m", "man"]})
FALLBACKS = ["%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%Y%m%d", "%d-%m-%y"]
SWAPPED = {"%d/%m/%Y": "%m/%d/%Y", "%m/%d/%Y": "%d/%m/%Y"}
GRADES = {"G5", "P3", "D1", "Non-staff"}


# --- text -------------------------------------------------------------------
def test_clean_text_trims_and_collapses_spaces():
    assert st.clean_text("  Mary   Ann ") == ("Mary Ann", st.CORRECTED)

def test_clean_text_unchanged_is_ok():
    assert st.clean_text("Mary") == ("Mary", st.OK)

def test_clean_text_blank_is_missing():
    assert st.clean_text("   ") == (None, st.MISSING)
    assert st.clean_text(None) == (None, st.MISSING)


# --- categories ---------------------------------------------------------------
def test_category_variants_are_mapped():
    assert st.map_category("F", GENDER) == ("Female", st.CORRECTED)
    assert st.map_category(" woman ", GENDER) == ("Female", st.CORRECTED)

def test_category_canonical_value_is_ok():
    assert st.map_category("Male", GENDER) == ("Male", st.OK)

def test_category_unknown_is_invalid():
    assert st.map_category("Unknown", GENDER) == ("Unknown", st.INVALID)


# --- dates --------------------------------------------------------------------
def test_date_in_office_format_is_ok():
    assert st.parse_date("14/03/2025", "%d/%m/%Y", FALLBACKS, SWAPPED) == (date(2025, 3, 14), st.OK)

def test_us_office_reads_month_first():
    # The same text means a different date in Panama (month first)
    assert st.parse_date("03/04/2025", "%m/%d/%Y", FALLBACKS, SWAPPED) == (date(2025, 3, 4), st.OK)
    assert st.parse_date("03/04/2025", "%d/%m/%Y", FALLBACKS, SWAPPED) == (date(2025, 4, 3), st.OK)

def test_alternative_format_is_corrected():
    assert st.parse_date("14 March 2025", "%d/%m/%Y", FALLBACKS, SWAPPED) == (date(2025, 3, 14), st.CORRECTED)
    assert st.parse_date("20250314", "%d/%m/%Y", FALLBACKS, SWAPPED) == (date(2025, 3, 14), st.CORRECTED)

def test_swapped_day_month_detected_when_day_above_12():
    # 03/25/2025 cannot be day-first (no month 25), so the swap is the only valid reading
    assert st.parse_date("03/25/2025", "%d/%m/%Y", FALLBACKS, SWAPPED) == (date(2025, 3, 25), st.CORRECTED)

def test_impossible_date_is_invalid():
    assert st.parse_date("30/02/2025", "%d/%m/%Y", FALLBACKS, SWAPPED)[1] == st.INVALID
    assert st.parse_date("2025-04-31", "%Y-%m-%d", FALLBACKS, SWAPPED)[1] == st.INVALID


# --- months, grades, counts ----------------------------------------------------
def test_reporting_month_formats():
    formats = ["%Y-%m", "%b-%Y", "%m/%Y"]
    assert st.parse_month("2024-11", formats) == ("2024-11", st.OK)
    assert st.parse_month("Nov-2024", formats) == ("2024-11", st.CORRECTED)
    assert st.parse_month("11/2024", formats) == ("2024-11", st.CORRECTED)

def test_grade_normalisation():
    assert st.normalise_grade("P3", GRADES) == ("P3", st.OK)
    assert st.normalise_grade("p3", GRADES) == ("P3", st.CORRECTED)
    assert st.normalise_grade("G-5", GRADES) == ("G5", st.CORRECTED)
    assert st.normalise_grade("P6", GRADES) == ("P6", st.INVALID)

def test_counts():
    assert st.parse_count("120") == (120, st.OK)
    assert st.parse_count("N/A")[1] == st.INVALID
    assert st.parse_count("-12")[1] == st.INVALID
    assert st.parse_count("twelve")[1] == st.INVALID
