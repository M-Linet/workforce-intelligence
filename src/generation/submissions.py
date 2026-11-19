"""
Write the clean 'true world' out as the files each office would submit.

Every office has its own habits (from config/organisation.yaml):
  file_format  : csv or xlsx
  date_format  : e.g. %d/%m/%Y or %m/%d/%Y
  column_style : snake_case, title_case or abbreviated

The work is split in two so data-quality problems can be injected IN BETWEEN:
  1. build_submissions()  -> a list of Submission objects (one per file),
                             still using the canonical column names
  2. write_submissions()  -> applies each office's formatting and writes files

Folder layout produced (data/raw/):
  HQ/departments.xlsx, HQ/offices.csv
  OFF01/OFF01_workforce_snapshot_2024-11.csv
  OFF01/OFF01_movements_2024-11.csv
  OFF01/OFF01_recruitment_2024-11.csv
  ...
  OFF01/OFF01_employee_master_2026-10.csv
"""

import shutil
from dataclasses import dataclass

import pandas as pd

from src.config import RAW_DIR, load_yaml
from src.generation.reference_data import TRUTH_DIR

# Columns holding full dates (formatted with each office's date_format)
DATE_COLUMNS = {
    "employee_master": ["date_of_birth", "contract_start_date", "contract_end_date", "hire_date", "last_updated"],
    "movements": ["effective_date"],
    "recruitment": ["vacancy_open_date", "vacancy_close_date"],
    "workforce_snapshot": [],
}


@dataclass
class Submission:
    office_id: str
    dataset: str
    month: str               # reporting month, 'YYYY-MM'
    data: pd.DataFrame       # canonical column names, ISO date strings


# ---------------------------------------------------------------------------
# 1. Split the truth tables into per-office, per-month submissions
# ---------------------------------------------------------------------------
def build_submissions(cfg: dict, formats: dict) -> list[Submission]:
    gen = cfg["generation"]
    months = [p.strftime("%Y-%m") for p in
              pd.period_range(gen["first_reporting_month"], gen["last_reporting_month"], freq="M")]
    offices = [o["office_id"] for o in cfg["offices"]]
    cols = formats["datasets"]

    snaps = pd.read_csv(TRUTH_DIR / "snapshots.csv", dtype=str)
    moves = pd.read_csv(TRUTH_DIR / "movements.csv", dtype=str)
    recs = pd.read_csv(TRUTH_DIR / "recruitment.csv", dtype=str)
    master = pd.read_csv(TRUTH_DIR / "employees_master.csv", dtype=str)

    # Which month each record is reported in
    moves["_month"] = moves["effective_date"].str[:7]
    # Vacancies are reported in the month they close; still-open ones in the final month.
    # Vacancies that closed BEFORE the period (for hires early in the period) are
    # reported as a backlog in the first month's return.
    close_month = recs["vacancy_close_date"].str[:7].fillna(months[-1])
    recs["_month"] = close_month.where(close_month >= months[0], months[0])

    subs = []
    for office in offices:
        for m in months:
            subs.append(Submission(office, "workforce_snapshot", m,
                snaps[(snaps["office_id"] == office) & (snaps["reporting_month"] == m)][cols["workforce_snapshot"]]))
            # The office where the employee ends up reports the movement (receiving office for transfers)
            subs.append(Submission(office, "movements", m,
                moves[(moves["new_location"] == office) & (moves["_month"] == m)][cols["movements"]]))
            subs.append(Submission(office, "recruitment", m,
                recs[(recs["office_id"] == office) & (recs["_month"] == m)][cols["recruitment"]]))
        subs.append(Submission(office, "employee_master", months[-1],
            master[master["source_office"] == office][cols["employee_master"]]))

    return [Submission(s.office_id, s.dataset, s.month, s.data.reset_index(drop=True)) for s in subs]


# ---------------------------------------------------------------------------
# 2. Apply each office's formatting and write the files
# ---------------------------------------------------------------------------
def rename_columns(columns, style: str, abbreviations: dict) -> list[str]:
    if style == "snake_case":
        return list(columns)
    if style == "title_case":
        return [c.replace("_", " ").title() for c in columns]      # employee_id -> Employee Id
    if style == "abbreviated":
        return [abbreviations.get(c, c) for c in columns]
    raise ValueError(f"Unknown column style: {style}")


def month_format(date_format: str) -> str:
    """Offices write the reporting month in a style matching their date habit."""
    if "%b" in date_format:
        return "%b-%Y"           # Nov-2024
    if date_format.startswith("%Y"):
        return "%Y-%m"           # 2024-11
    return "%m/%Y"               # 11/2024


def reformat_dates(series: pd.Series, fmt: str) -> pd.Series:
    """Convert ISO date strings to the office's format. Values that are not
    valid ISO dates (for example, injected errors) are left exactly as they are."""
    parsed = pd.to_datetime(series, format="%Y-%m-%d", errors="coerce")
    return parsed.dt.strftime(fmt).where(parsed.notna(), series)


def format_for_office(sub: Submission, office_cfg: dict, abbreviations: dict) -> pd.DataFrame:
    df = sub.data.copy()
    for col in DATE_COLUMNS[sub.dataset]:
        if col in df.columns:
            df[col] = reformat_dates(df[col], office_cfg["date_format"])
    if "reporting_month" in df.columns:
        parsed = pd.to_datetime(df["reporting_month"], format="%Y-%m", errors="coerce")
        df["reporting_month"] = parsed.dt.strftime(month_format(office_cfg["date_format"])).where(
            parsed.notna(), df["reporting_month"])
    df.columns = rename_columns(df.columns, office_cfg["column_style"], abbreviations)
    return df


def write_table(df: pd.DataFrame, path, file_format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if file_format == "csv":
        df.to_csv(path, index=False, encoding="utf-8")
    elif file_format == "xlsx":
        df.to_excel(path, index=False, engine="openpyxl")
    else:
        raise ValueError(f"Unknown file format: {file_format}")


def clear_raw_folder() -> None:
    """Remove previously generated submissions so reruns start clean.
    Only folders this generator creates are removed; .gitkeep is kept."""
    for folder in RAW_DIR.iterdir() if RAW_DIR.exists() else []:
        if folder.is_dir() and (folder.name == "HQ" or folder.name.startswith("OFF")):
            shutil.rmtree(folder)


def write_submissions(cfg: dict, formats: dict, subs: list[Submission]) -> pd.DataFrame:
    """Write every submission plus the HQ reference files. Returns a file inventory."""
    clear_raw_folder()
    office_cfg = {o["office_id"]: o for o in cfg["offices"]}
    abbreviations = formats["abbreviations"]
    inventory = []

    # HQ reference data
    hq = RAW_DIR / "HQ"
    write_table(pd.read_csv(TRUTH_DIR / "departments.csv", dtype=str), hq / "departments.xlsx", "xlsx")
    write_table(pd.read_csv(TRUTH_DIR / "offices.csv", dtype=str), hq / "offices.csv", "csv")
    inventory += [{"office_id": "HQ", "dataset": "departments", "month": None, "rows": 15, "file": "HQ/departments.xlsx"},
                  {"office_id": "HQ", "dataset": "offices", "month": None, "rows": 10, "file": "HQ/offices.csv"}]

    for sub in subs:
        oc = office_cfg[sub.office_id]
        path = RAW_DIR / sub.office_id / f"{sub.office_id}_{sub.dataset}_{sub.month}.{oc['file_format']}"
        write_table(format_for_office(sub, oc, abbreviations), path, oc["file_format"])
        inventory.append({"office_id": sub.office_id, "dataset": sub.dataset, "month": sub.month,
                          "rows": len(sub.data), "file": str(path.relative_to(RAW_DIR))})

    return pd.DataFrame(inventory)


def main() -> None:
    cfg = load_yaml("organisation.yaml")
    formats = load_yaml("submission_formats.yaml")

    subs = build_submissions(cfg, formats)
    inventory = write_submissions(cfg, formats, subs)

    print(f"Files written: {len(inventory)} -> {RAW_DIR}\n")
    print("Files and rows by dataset:")
    print(inventory.groupby("dataset").agg(files=("file", "count"), rows=("rows", "sum")).to_string(), "\n")
    print(f"Empty files (0 data rows): {(inventory['rows'] == 0).sum()}\n")

    # Reconciliation: every truth record must appear in exactly one submission
    expected = {"workforce_snapshot": len(pd.read_csv(TRUTH_DIR / "snapshots.csv")),
                "movements": len(pd.read_csv(TRUTH_DIR / "movements.csv")),
                "recruitment": len(pd.read_csv(TRUTH_DIR / "recruitment.csv")),
                "employee_master": len(pd.read_csv(TRUTH_DIR / "employees_master.csv"))}
    written = inventory.groupby("dataset")["rows"].sum()
    print("Checks (rows written = rows in truth):")
    for ds, n in expected.items():
        print(f"  {ds:<20} {written[ds] == n}  ({written[ds]} vs {n})")


if __name__ == "__main__":
    main()