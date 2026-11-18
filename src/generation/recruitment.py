"""
Generate recruitment (vacancy) records that are consistent with actual hires.

Design:
  - Every STAFF hire during the period (Fixed-term, Temporary, Continuing)
    becomes a 'Filled' vacancy: same office, department and job title, closing
    shortly before the hire date. Consultants and interns are engaged through
    separate processes and are not part of the vacancy data.
  - The remaining records are 'Cancelled', 'Unsuccessful' or still 'Open'.
  - Some filled vacancies had an earlier unsuccessful round. Both rounds share
    the same vacancy_id but have different recruitment_ids. That is why the
    data has two identifiers.

Funnel rule (must always hold):
  applications >= shortlisted >= interviewed >= offers >= hired

Output: data/synthetic/truth/recruitment.csv
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.config import load_yaml
from src.generation.employees import JOB_AREA, TITLE_PATTERN, weighted_choice
from src.generation.movements import add_months, month_starts
from src.generation.reference_data import TRUTH_DIR, build_departments

RECRUITED_TYPES = {"Fixed-term", "Temporary", "Continuing"}

# Days from vacancy opening to selection, by grade band: (min, max)
DAYS_TO_FILL = {"G": (30, 90), "P": (60, 150), "D": (90, 180), "Temporary": (20, 60)}
# Some offices recruit more slowly than others (simulation parameter)
OFFICE_SPEED = {"OFF05": 0.9, "OFF07": 1.2, "OFF09": 1.15, "OFF10": 1.3}
ONBOARDING_DAYS = (14, 45)          # selection -> first day at work

# Mean number of applications, by grade band
MEAN_APPLICATIONS = {"G": 60, "P": 120, "D": 80, "Temporary": 30}

N_REROUND = 40                      # filled vacancies that had an earlier failed round


def band(grade: str, emp_type: str) -> str:
    if emp_type == "Temporary":
        return "Temporary"
    return grade[0]                 # 'G', 'P' or 'D'


def days_to_fill(rng, grade, emp_type, office_id):
    lo, hi = DAYS_TO_FILL[band(grade, emp_type)]
    return int(rng.integers(lo, hi + 1) * OFFICE_SPEED.get(office_id, 1.0))


def funnel(rng, grade, emp_type, stage):
    """Return (applications, shortlisted, interviewed, offers, hired) for a process
    that reached `stage`: 'applied', 'shortlisted', 'interviewed', 'offer_declined' or 'hired'."""
    mean = MEAN_APPLICATIONS[band(grade, emp_type)]
    apps = max(5, int(rng.lognormal(np.log(mean), 0.5)))
    short = interv = offers = hired = 0
    if stage != "applied":
        short = int(min(apps, max(3, rng.binomial(apps, 0.12))))
    if stage in {"interviewed", "offer_declined", "hired"}:
        interv = int(min(short, max(2, rng.binomial(short, 0.6))))
    if stage == "offer_declined":
        offers = 1
    if stage == "hired":
        offers = 2 if rng.random() < 0.10 else 1      # first choice sometimes declines
        offers = min(offers, interv)
        hired = 1
    return apps, short, interv, offers, hired


def build_recruitment(cfg: dict, base: pd.DataFrame) -> pd.DataFrame:
    gen = cfg["generation"]
    rng = np.random.default_rng([gen["random_seed"], 2])
    months = month_starts(gen["first_reporting_month"], gen["last_reporting_month"])
    period_start, period_end = months[0], add_months(months[-1], 1) - timedelta(days=1)

    departments = build_departments(cfg).set_index("department_id")
    dept_weights = {d: (2 if r["department_category"] == "Programme" else 1) for d, r in departments.iterrows()}
    office_weights = {o["office_id"]: o["headcount_weight"] for o in cfg["offices"]}
    staff_grades = cfg["grades"]

    records = []

    # ---- 1. Filled vacancies: one per staff hire during the period -----------
    hires = base[(base["hire_date"] >= str(period_start)) & base["employment_type"].isin(RECRUITED_TYPES)]
    for h in hires.itertuples(index=False):
        hire = date.fromisoformat(h.hire_date)
        close = hire - timedelta(days=int(rng.integers(*ONBOARDING_DAYS)))
        opened = close - timedelta(days=days_to_fill(rng, h.grade_level, h.employment_type, h.office_id))
        apps, short, interv, offers, hired = funnel(rng, h.grade_level, h.employment_type, "hired")
        records.append({
            "department_id": h.department_id, "office_id": h.office_id, "job_title": h.job_title,
            "grade_level": h.grade_level, "employment_type": h.employment_type,
            "vacancy_open_date": opened, "vacancy_close_date": close,
            "application_count": apps, "shortlisted_count": short, "interviewed_count": interv,
            "offers_made": offers, "candidates_hired": hired,
            "recruitment_status": "Filled", "hired_employee_id": h.employee_id,
            "post": len(records),   # identifies the post; re-advertisements share it
        })

    # ---- 2. Earlier unsuccessful rounds for some filled vacancies -------------
    filled_idx = rng.choice(len(records), size=N_REROUND, replace=False)
    for i in sorted(filled_idx):
        f = records[i]
        close = f["vacancy_open_date"] - timedelta(days=int(rng.integers(1, 15)))
        opened = close - timedelta(days=days_to_fill(rng, f["grade_level"], f["employment_type"], f["office_id"]))
        stage = "offer_declined" if rng.random() < 0.3 else "interviewed"
        apps, short, interv, offers, hired = funnel(rng, f["grade_level"], f["employment_type"], stage)
        records.append({
            **f, "vacancy_open_date": opened, "vacancy_close_date": close,
            "application_count": apps, "shortlisted_count": short, "interviewed_count": interv,
            "offers_made": offers, "candidates_hired": hired,
            "recruitment_status": "Unsuccessful", "hired_employee_id": None,
        })                            # 'post' is copied from the filled record via **f

    # ---- 3. Other vacancies: cancelled, unsuccessful, or still open ----------
    remaining = gen["total_recruitments"] - len(records)
    n_open = remaining // 3
    statuses = ["Open"] * n_open + ["Cancelled", "Unsuccessful"] * ((remaining - n_open) // 2)
    statuses += ["Cancelled"] * (remaining - len(statuses))
    for status in statuses:
        office_id = weighted_choice(rng, office_weights)
        dept_id = weighted_choice(rng, dept_weights)
        emp_type = weighted_choice(rng, {"Fixed-term": 60, "Temporary": 25, "Continuing": 15})
        grade = weighted_choice(rng, staff_grades)
        dept = departments.loc[dept_id]
        title = TITLE_PATTERN[grade].format(a=JOB_AREA[dept_id], d=dept["department_name"], r=dept["directorate"])

        if status == "Open":
            # Opened in the last 4 months, not yet closed
            opened = period_end - timedelta(days=int(rng.integers(0, 120)))
            close = None
            stage = "applied" if (period_end - opened).days < 45 else "shortlisted"
        else:
            opened = period_start + timedelta(days=int(rng.integers(0, (period_end - period_start).days - 200)))
            if status == "Cancelled":
                close = opened + timedelta(days=int(rng.integers(10, 90)))
                stage = rng.choice(["applied", "shortlisted"])
            else:  # Unsuccessful: went through interviews, nobody appointed
                close = opened + timedelta(days=days_to_fill(rng, grade, emp_type, office_id))
                stage = "offer_declined" if rng.random() < 0.3 else "interviewed"
        apps, short, interv, offers, hired = funnel(rng, grade, emp_type, stage)
        records.append({
            "department_id": dept_id, "office_id": office_id, "job_title": title,
            "grade_level": grade, "employment_type": emp_type,
            "vacancy_open_date": opened, "vacancy_close_date": close,
            "application_count": apps, "shortlisted_count": short, "interviewed_count": interv,
            "offers_made": offers, "candidates_hired": hired,
            "recruitment_status": status, "hired_employee_id": None,
            "post": len(records),
        })

    # ---- IDs: vacancy_id is shared by rounds of the same post ----------------
    df = pd.DataFrame(records)
    # Number posts in order of when they were first advertised: VA-<year>-<sequence>
    first_open = df.groupby("post")["vacancy_open_date"].min().sort_values(kind="stable")
    vacancy_ids = {post: f"VA-{opened.year}-{n:04d}" for n, (post, opened) in enumerate(first_open.items(), start=1)}
    df["vacancy_id"] = df["post"].map(vacancy_ids)
    df = df.sort_values(["vacancy_open_date", "vacancy_id"]).reset_index(drop=True)
    df.insert(0, "recruitment_id", [f"R{i:05d}" for i in range(1, len(df) + 1)])

    columns = ["recruitment_id", "vacancy_id", "department_id", "office_id", "job_title",
               "vacancy_open_date", "vacancy_close_date", "application_count", "shortlisted_count",
               "interviewed_count", "offers_made", "candidates_hired", "recruitment_status",
               "grade_level", "employment_type", "hired_employee_id"]
    return df[columns]


def main() -> None:
    cfg = load_yaml("organisation.yaml")
    base = pd.read_csv(TRUTH_DIR / "employees_base.csv", dtype=str)

    rec = build_recruitment(cfg, base)
    out = TRUTH_DIR / "recruitment.csv"
    rec.to_csv(out, index=False, encoding="utf-8")

    filled = rec[rec["recruitment_status"] == "Filled"]
    days = (pd.to_datetime(filled["vacancy_close_date"]) - pd.to_datetime(filled["vacancy_open_date"])).dt.days
    closed = rec[rec["recruitment_status"] != "Open"]
    f = rec[["application_count", "shortlisted_count", "interviewed_count", "offers_made", "candidates_hired"]]

    print(f"Recruitment: {len(rec)} rows -> {out}\n")
    print("By status:")
    print(rec["recruitment_status"].value_counts().to_string(), "\n")
    print(f"Fill rate (filled / closed):        {len(filled) / len(closed):.1%}")
    print(f"Average days open -> selection:     {days.mean():.0f}")
    print(f"Vacancies with more than one round: {(rec['vacancy_id'].value_counts() > 1).sum()}\n")
    print("Funnel totals:")
    print(f.sum().to_string(), "\n")

    print("Checks:")
    monotonic = ((f.iloc[:, 0] >= f.iloc[:, 1]) & (f.iloc[:, 1] >= f.iloc[:, 2])
                 & (f.iloc[:, 2] >= f.iloc[:, 3]) & (f.iloc[:, 3] >= f.iloc[:, 4])).all()
    print(f"  Funnel never increases:            {monotonic}")
    print(f"  Close date after open date:        {(pd.to_datetime(closed['vacancy_close_date']) > pd.to_datetime(closed['vacancy_open_date'])).all()}")
    print(f"  Open vacancies have no close date: {rec.loc[rec['recruitment_status'] == 'Open', 'vacancy_close_date'].isna().all()}")
    print(f"  Each staff hire filled once:       {filled['hired_employee_id'].is_unique}")
    print(f"  Recruitment IDs unique:            {rec['recruitment_id'].is_unique}")


if __name__ == "__main__":
    main()