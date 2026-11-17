"""
Build clean reference tables (departments, offices) from the organisation config.

These form the 'ground truth' that the rest of the synthetic data hangs off.
Output: data/synthetic/truth/departments.csv and offices.csv
"""

import pandas as pd

from src.config import SYNTHETIC_DIR, load_yaml

TRUTH_DIR = SYNTHETIC_DIR / "truth"

DEPARTMENT_COLUMNS = ["department_id", "department_name", "department_category", "directorate"]
OFFICE_COLUMNS = ["office_id", "office_name", "country", "region", "city", "office_type"]


def build_departments(cfg: dict) -> pd.DataFrame:
    """Return a DataFrame of departments from the config."""
    df = pd.DataFrame(cfg["departments"])
    return df[DEPARTMENT_COLUMNS]


def build_offices(cfg: dict) -> pd.DataFrame:
    """Return a DataFrame of offices (master-data columns only).

    The config also holds generation settings for each office (date_format,
    error rates, and so on). Those describe how the office *submits* data, not
    facts about the office, so they are deliberately left out of this table.
    """
    df = pd.DataFrame(cfg["offices"])
    return df[OFFICE_COLUMNS]


def main() -> None:
    cfg = load_yaml("organisation.yaml")
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)

    departments = build_departments(cfg)
    offices = build_offices(cfg)

    departments.to_csv(TRUTH_DIR / "departments.csv", index=False)
    offices.to_csv(TRUTH_DIR / "offices.csv", index=False)

    print(f"Departments: {len(departments)} rows -> {TRUTH_DIR / 'departments.csv'}")
    print(f"Offices:     {len(offices)} rows -> {TRUTH_DIR / 'offices.csv'}")
    print()
    print(offices[["office_id", "city", "region"]].to_string(index=False))


if __name__ == "__main__":
    main()