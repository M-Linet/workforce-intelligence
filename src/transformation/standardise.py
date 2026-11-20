"""
Small, pure functions that clean ONE value at a time.

Each returns (clean_value, outcome) where outcome is one of:
  'ok'        value was already correct
  'corrected' value was fixed by a documented rule (see config/standardisation.yaml)
  'missing'   value was empty
  'invalid'   value could not be interpreted

Pure functions (no database, no files) are easy to unit-test.
"""

from datetime import date, datetime

import pandas as pd

OK, CORRECTED, MISSING, INVALID = "ok", "corrected", "missing", "invalid"


def is_blank(value) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or \
        value is pd.NA or str(value).strip() == ""


def clean_text(value):
    """Trim and collapse repeated inner spaces: '  Mary   Ann ' -> 'Mary Ann'."""
    if is_blank(value):
        return None, MISSING
    cleaned = " ".join(str(value).split())
    return cleaned, (OK if cleaned == value else CORRECTED)


def build_lookup(value_map: dict) -> dict:
    """{'Female': ['f', 'woman']} -> {'female': 'Female', 'f': 'Female', 'woman': 'Female'}"""
    lookup = {}
    for canonical, variants in value_map.items():
        lookup[canonical.casefold()] = canonical
        for v in variants:
            lookup[str(v).casefold()] = canonical
    return lookup


def map_category(value, lookup: dict):
    if is_blank(value):
        return None, MISSING
    raw = str(value)
    canonical = lookup.get(" ".join(raw.split()).casefold())
    if canonical is None:
        return raw, INVALID
    return canonical, (OK if canonical == raw else CORRECTED)


def _try(value: str, fmt: str):
    try:
        return datetime.strptime(value, fmt).date()
    except ValueError:
        return None


def parse_date(value, office_format: str, fallbacks: list, swapped: dict):
    """Parse with the office's registered format first.

    If that fails, try unambiguous alternative formats, then (last) the
    day/month swap. A date written 03/04/2025 in the wrong convention parses
    successfully with the office format and CANNOT be detected here; that is
    a documented limitation.
    """
    if is_blank(value):
        return None, MISSING
    text = str(value).strip()
    parsed = _try(text, office_format)
    if parsed:
        return parsed, OK
    for fmt in fallbacks + ([swapped[office_format]] if office_format in swapped else []):
        parsed = _try(text, fmt)
        if parsed:
            return parsed, CORRECTED
    return text, INVALID


def parse_month(value, formats: list):
    """'Nov-2024', '11/2024', '2024-11' -> '2024-11'."""
    if is_blank(value):
        return None, MISSING
    text = str(value).strip()
    for i, fmt in enumerate(formats):
        parsed = _try(text, fmt)
        if parsed:
            return parsed.strftime("%Y-%m"), (OK if text == parsed.strftime("%Y-%m") else CORRECTED)
    return text, INVALID


def normalise_grade(value, valid: set):
    """'p3' -> 'P3', 'G-5' -> 'G5'. 'P6' stays invalid."""
    if is_blank(value):
        return None, MISSING
    raw = str(value)
    if raw in valid:
        return raw, OK
    candidate = raw.strip().upper().replace("-", "").replace(" ", "")
    candidate = "Non-staff" if candidate == "NONSTAFF" else candidate
    return (candidate, CORRECTED) if candidate in valid else (raw, INVALID)


def parse_count(value):
    """Whole, non-negative number. 'N/A', 'twelve', '-12' are invalid."""
    if is_blank(value):
        return None, MISSING
    text = str(value).strip()
    if text.isdigit():
        return int(text), OK
    return text, INVALID
