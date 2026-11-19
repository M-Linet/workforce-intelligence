"""
Inject realistic data-quality problems into office submissions, and record
every single one in a CORRUPTION MANIFEST.

The manifest is the answer key. The ETL pipeline never reads it. It is used
only afterwards, to measure how many injected problems the pipeline caught.

Three levels of problems:
  1. Record level : one value in one row is damaged (wrong format, typo, blank...)
  2. Cross-file   : the same transfer is reported by BOTH offices involved
  3. File level   : unreadable files, completely empty files, re-sent files

How often? Each office has error_rate_start and error_rate_end in
organisation.yaml. The rate for a month is interpolated between the two,
so data quality improves over time (a simulation parameter, not a finding).

Outputs:
  data/raw/...                                    the damaged submissions
  data/synthetic/corruption_manifest.csv          one row per injected problem
"""

import shutil
from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.config import RAW_DIR, SYNTHETIC_DIR, load_yaml
from src.generation.reference_data import TRUTH_DIR, build_departments
from src.generation.submissions import Submission, build_submissions, write_submissions

MANIFEST_PATH = SYNTHETIC_DIR / "corruption_manifest.csv"

# ---------------------------------------------------------------------------
# Which problems can occur in which dataset, and how often (relative weights)
# ---------------------------------------------------------------------------
CATALOGUE = {
    "employee_master": {
        "duplicate_row": 6, "stale_duplicate": 6, "country_variant": 12, "gender_variant": 10,
        "employment_type_variant": 8, "missing_employment_type": 6, "missing_department": 6,
        "department_name_not_code": 6, "extra_whitespace": 12, "alternative_date_format": 8,
        "impossible_date": 4, "date_sequence_error": 6, "invalid_grade": 4, "missing_primary_key": 2,
    },
    "workforce_snapshot": {
        "duplicate_row": 10, "missing_department": 15, "unknown_department_code": 10,
        "status_variant": 25, "invalid_grade": 15, "missing_primary_key": 5, "unknown_employee": 20,
    },
    "movements": {
        "duplicate_row": 10, "movement_type_variant": 25, "alternative_date_format": 20,
        "impossible_date": 10, "missing_department": 15, "unknown_employee": 20,
    },
    "recruitment": {
        "duplicate_row": 8, "missing_recruitment_date": 15, "date_sequence_error": 15,
        "invalid_number": 30, "missing_department": 12, "alternative_date_format": 20,
    },
}

DATE_FIELDS = {
    "employee_master": ["date_of_birth", "hire_date", "contract_start_date", "last_updated"],
    "movements": ["effective_date"],
    "recruitment": ["vacancy_open_date", "vacancy_close_date"],
}
PRIMARY_KEY = {"employee_master": "employee_id", "workforce_snapshot": "snapshot_id",
               "movements": "movement_id", "recruitment": "recruitment_id"}

# ---------------------------------------------------------------------------
# Realistic variants that real offices type
# ---------------------------------------------------------------------------
COUNTRY_ALIASES = {
    "Kenya": ["Republic of Kenya", "KE"],
    "Ethiopia": ["Ethopia", "Federal Republic of Ethiopia"],
    "Senegal": ["Sénégal", "Republic of Senegal"],
    "South Africa": ["RSA", "S. Africa"],
    "Switzerland": ["Suisse", "Schweiz", "CH"],
    "Thailand": ["Kingdom of Thailand", "Thailande"],
    "Bangladesh": ["Bangla Desh", "BD"],
    "Jordan": ["Hashemite Kingdom of Jordan", "Jordon"],
    "Panama": ["Panamá", "Republic of Panama"],
    "Democratic Republic of the Congo": ["DRC", "DR Congo", "Congo, Dem. Rep.", "Congo-Kinshasa"],
    "United Kingdom": ["UK", "Great Britain"],
    "Tanzania": ["United Republic of Tanzania"],
}
GENDER_VARIANTS = {
    "Female": ["F", "female", "FEMALE", "Woman", "f"],
    "Male": ["M", "male", "MALE", "Man", "m"],
    "Non-binary / Not disclosed": ["Not disclosed", "Other", "X", "Prefer not to say"],
}
EMPLOYMENT_TYPE_VARIANTS = {
    "Fixed-term": ["Fixed term", "FT", "fixed-term", "Fixed Term Appointment"],
    "Continuing": ["Permanent", "continuing", "CONTINUING"],
    "Temporary": ["Temp", "temporary", "TA"],
    "Consultant": ["consultant", "Individual Contractor"],
    "Intern": ["intern", "Internship"],
}
MOVEMENT_TYPE_VARIANTS = {
    "Resignation": ["Resigned", "resignation"],
    "Contract renewal": ["Renewal", "Contract Renewal", "contract renewal"],
    "Contract expiry": ["End of contract", "Contract ended", "Expiry"],
    "Promotion": ["promotion", "Promoted"],
    "Transfer": ["transfer", "Reassignment"],
    "Retirement": ["Retired", "retirement"],
    "Termination": ["Terminated", "Dismissal"],
}
STATUS_VARIANTS = {"Active": ["active", "ACTIVE", "Current"], "Separated": ["Left", "separated", "Inactive"]}
# Formats that are NOT ISO, so the writer passes them through unchanged
ALTERNATIVE_DATE_FORMATS = ["%d %B %Y", "%B %d, %Y", "%Y%m%d", "%d-%m-%y"]


class Injector:
    """Holds the random generator and manifest, and applies one problem at a time."""

    def __init__(self, rng, offices_cfg, departments):
        self.rng = rng
        self.offices = offices_cfg
        self.dept_names = departments.set_index("department_id")["department_name"].to_dict()
        self.manifest = []

    def pick(self, options):
        return options[self.rng.integers(len(options))]

    def log(self, sub, key, column, issue, original, corrupted, file_level=False):
        self.manifest.append({
            "office_id": sub.office_id, "dataset": sub.dataset, "month": sub.month,
            "record_key": None if file_level else key, "column": column, "issue_type": issue,
            "original_value": original, "corrupted_value": corrupted,
        })

    # ---- helpers for dates in the office's own format -----------------------
    def office_format(self, sub):
        return self.offices[sub.office_id]["date_format"]

    def impossible_date(self, iso_value, fmt):
        """Build an impossible date (e.g. 30 February) written in the office's format."""
        year = iso_value[:4]
        day, month, mon = self.pick([("30", "02", "Feb"), ("31", "04", "Apr"), ("31", "09", "Sep"), ("32", "01", "Jan")])
        return fmt.replace("%d", day).replace("%m", month).replace("%b", mon).replace("%Y", year)

    def alternative_date(self, iso_value, fmt):
        d = date.fromisoformat(iso_value)
        options = list(ALTERNATIVE_DATE_FORMATS)
        if "%d/%m" in fmt:
            options.append("%m/%d/%Y")          # day and month swapped
        elif "%m/%d" in fmt:
            options.append("%d/%m/%Y")
        return d.strftime(self.pick(options))

    # ---- record-level problems ------------------------------------------------
    def corrupt_row(self, sub, row: dict, extra_rows: list):
        """Apply one randomly chosen problem to `row` (in place). Returns nothing."""
        ds = sub.dataset
        key = row.get(PRIMARY_KEY[ds])
        weights = CATALOGUE[ds]
        issues = list(weights)
        p = np.array(list(weights.values()), dtype=float)
        issue = issues[self.rng.choice(len(issues), p=p / p.sum())]

        def set_value(col, new, issue_name=issue):
            self.log(sub, key, col, issue_name, row.get(col), new)
            row[col] = new

        def has(col):
            return pd.notna(row.get(col)) and str(row.get(col)) != ""

        if issue == "duplicate_row":
            extra_rows.append(dict(row))
            self.log(sub, key, None, issue, None, None)

        elif issue == "stale_duplicate":
            # An older version of the same employee's record is sent again
            stale = dict(row)
            old_date = date.fromisoformat(row["last_updated"]) - timedelta(days=int(self.rng.integers(60, 400)))
            stale["last_updated"] = old_date.isoformat()
            others = [d for d in self.dept_names if d != row["department_id"]]
            stale["department_id"] = self.pick(others)
            extra_rows.append(stale)
            self.log(sub, key, "department_id", issue, row["department_id"], stale["department_id"])

        elif issue == "country_variant":
            col = self.pick(["country_of_assignment", "nationality"])
            value = row[col]
            options = COUNTRY_ALIASES.get(value, []) + [value.upper(), value.lower(), f" {value} "]
            set_value(col, self.pick(options))

        elif issue == "gender_variant":
            set_value("gender", self.pick(GENDER_VARIANTS[row["gender"]]))

        elif issue == "employment_type_variant":
            set_value("employment_type", self.pick(EMPLOYMENT_TYPE_VARIANTS[row["employment_type"]]))

        elif issue == "missing_employment_type":
            set_value("employment_type", None)

        elif issue == "missing_department":
            col = "new_department" if ds == "movements" else "department_id"
            set_value(col, None)

        elif issue == "department_name_not_code":
            set_value("department_id", self.dept_names[row["department_id"]])

        elif issue == "unknown_department_code":
            set_value("department_id", self.pick(["DEP99", "DEP00", "DEP16", "D01"]))

        elif issue == "extra_whitespace":
            col = self.pick(["first_name", "last_name", "job_title"])
            value = row[col]
            set_value(col, self.pick([f"  {value}", f"{value}  ", value.replace(" ", "  ", 1) if " " in value else f" {value} "]))

        elif issue in {"alternative_date_format", "impossible_date"}:
            candidates = [c for c in DATE_FIELDS[ds] if has(c)]
            if not candidates:
                return
            col = self.pick(candidates)
            fmt = self.office_format(sub)
            new = self.alternative_date(row[col], fmt) if issue == "alternative_date_format" \
                else self.impossible_date(row[col], fmt)
            set_value(col, new)

        elif issue == "date_sequence_error":
            if ds == "recruitment":
                a, b = "vacancy_open_date", "vacancy_close_date"
            else:
                a, b = "contract_start_date", "contract_end_date"
            if not (has(a) and has(b)):
                return
            self.log(sub, key, f"{a}|{b}", issue, f"{row[a]}|{row[b]}", f"{row[b]}|{row[a]}")
            row[a], row[b] = row[b], row[a]

        elif issue == "invalid_grade":
            grade = row["grade_level"]
            if grade == "Non-staff":
                return
            set_value("grade_level", self.pick([grade.lower(), grade.replace("G", "G-").replace("P", "P-").replace("D", "D-"),
                                                "P6", "G9"]))

        elif issue == "missing_primary_key":
            set_value("employee_id", None)

        elif issue == "unknown_employee":
            set_value("employee_id", f"E{int(self.rng.integers(9000, 9999)):05d}")

        elif issue == "status_variant":
            set_value("employment_status", self.pick(STATUS_VARIANTS[row["employment_status"]]))

        elif issue == "movement_type_variant":
            set_value("movement_type", self.pick(MOVEMENT_TYPE_VARIANTS[row["movement_type"]]))

        elif issue == "missing_recruitment_date":
            set_value(self.pick(["vacancy_open_date", "vacancy_open_date", "vacancy_close_date"]), None)

        elif issue == "invalid_number":
            col = self.pick(["application_count", "shortlisted_count", "interviewed_count"])
            value = int(row[col])
            kind = self.pick(["negative", "text", "outlier", "funnel_violation"])
            if kind == "negative" and value > 0:
                set_value(col, str(-value))
            elif kind == "text":
                set_value(col, self.pick(["N/A", "unknown", "TBC", "twelve"]))
            elif kind == "outlier":
                set_value("application_count", str(int(row["application_count"]) * 100))
            else:
                set_value("shortlisted_count", str(int(row["application_count"]) + int(self.rng.integers(5, 50))))

    def corrupt_submission(self, sub: Submission, error_rate: float) -> Submission:
        rows = sub.data.to_dict("records")
        extra = []
        for row in rows:
            if self.rng.random() < error_rate:
                self.corrupt_row(sub, row, extra)
        df = pd.DataFrame(rows + extra, columns=sub.data.columns)
        return Submission(sub.office_id, sub.dataset, sub.month, df)


def month_error_rate(office: dict, month: str, months: list) -> float:
    """Linear improvement from error_rate_start (first month) to error_rate_end (last month)."""
    t = months.index(month) / (len(months) - 1)
    return office["error_rate_start"] + t * (office["error_rate_end"] - office["error_rate_start"])


def add_cross_file_duplicates(inj: Injector, subs: list[Submission], share: float = 0.3):
    """Some transfers are reported by BOTH the sending and the receiving office."""
    index = {(s.office_id, s.dataset, s.month): i for i, s in enumerate(subs)}
    for s in list(subs):
        if s.dataset != "movements" or s.data.empty:
            continue
        transfers = s.data[s.data["movement_type"] == "Transfer"]
        for _, row in transfers.iterrows():
            sender = row["previous_location"]
            if sender == s.office_id or inj.rng.random() >= share:
                continue
            j = index[(sender, "movements", s.month)]
            target = subs[j]
            subs[j] = Submission(target.office_id, target.dataset, target.month,
                                 pd.concat([target.data, row.to_frame().T], ignore_index=True))
            inj.log(target, row["movement_id"], None, "cross_file_duplicate", s.office_id, sender)


def damage_files(inj: Injector, cfg: dict, subs: list[Submission]):
    """File-level problems, applied AFTER the files are written."""
    office_cfg = {o["office_id"]: o for o in cfg["offices"]}

    def path_of(s):
        return RAW_DIR / s.office_id / f"{s.office_id}_{s.dataset}_{s.month}.{office_cfg[s.office_id]['file_format']}"

    candidates = [s for s in subs if s.dataset != "employee_master" and not s.data.empty]
    chosen = [candidates[i] for i in inj.rng.choice(len(candidates), size=5, replace=False)]

    # Two unreadable files (e.g. a failed upload): random bytes instead of content
    for s in chosen[:2]:
        path_of(s).write_bytes(inj.rng.bytes(2048))
        inj.log(s, None, None, "corrupted_file", f"{len(s.data)} rows", "unreadable bytes", file_level=True)

    # One completely empty file: 0 bytes, not even a header row
    s = chosen[2]
    path_of(s).write_bytes(b"")
    inj.log(s, None, None, "zero_byte_file", f"{len(s.data)} rows", "0 bytes", file_level=True)

    # Two files re-sent under a new name with identical content
    for s in chosen[3:5]:
        p = path_of(s)
        resent = p.with_name(p.stem + "_resubmitted" + p.suffix)
        shutil.copyfile(p, resent)
        inj.log(s, None, None, "resubmitted_file", p.name, resent.name, file_level=True)


def main() -> None:
    cfg = load_yaml("organisation.yaml")
    formats = load_yaml("submission_formats.yaml")
    gen = cfg["generation"]
    months = [p.strftime("%Y-%m") for p in
              pd.period_range(gen["first_reporting_month"], gen["last_reporting_month"], freq="M")]
    offices = {o["office_id"]: o for o in cfg["offices"]}

    inj = Injector(np.random.default_rng([gen["random_seed"], 3]), offices, build_departments(cfg))

    subs = build_submissions(cfg, formats)
    add_cross_file_duplicates(inj, subs)
    subs = [inj.corrupt_submission(s, month_error_rate(offices[s.office_id], s.month, months)) for s in subs]
    write_submissions(cfg, formats, subs)
    damage_files(inj, cfg, subs)

    manifest = pd.DataFrame(inj.manifest)
    manifest.insert(0, "issue_id", [f"Q{i:05d}" for i in range(1, len(manifest) + 1)])
    manifest.to_csv(MANIFEST_PATH, index=False, encoding="utf-8")

    print(f"Injected problems: {len(manifest)} -> {MANIFEST_PATH}\n")
    print("By dataset:")
    print(manifest["dataset"].value_counts().to_string(), "\n")
    print("By issue type:")
    print(manifest["issue_type"].value_counts().to_string(), "\n")

    # Per-office share of corrupted records, first 6 months vs last 6 months
    snaps = manifest[manifest["dataset"] == "workforce_snapshot"]
    early = snaps[snaps["month"] <= months[5]].groupby("office_id").size()
    late = snaps[snaps["month"] >= months[-6]].groupby("office_id").size()
    print("Snapshot problems per office, first 6 months vs last 6 months:")
    print(pd.DataFrame({"first_6": early, "last_6": late}).to_string())


if __name__ == "__main__":
    main()