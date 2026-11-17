"""
Generate the clean 'true' employee base table.

Each employee gets an office, department, grade, employment type,
nationality, name, gender, date of birth, hire date and job title.

This is the state of each employee ON THEIR HIRE DATE. Later steps add
movements (promotions, transfers, renewals, exits), derive the final
master record, and build monthly snapshots.

Output: data/synthetic/truth/employees_base.csv
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.config import load_yaml
from src.generation.reference_data import TRUTH_DIR, build_departments

# Short "work area" used to build job titles, e.g. "Senior Finance Officer"
JOB_AREA = {
    "DEP01": "Programme Management",
    "DEP02": "Urban Planning",
    "DEP03": "Housing and Land",
    "DEP04": "Climate Resilience",
    "DEP05": "Monitoring and Evaluation",
    "DEP06": "Data Analytics",
    "DEP07": "Research and Policy",
    "DEP08": "IT",
    "DEP09": "Human Resources",
    "DEP10": "Finance",
    "DEP11": "Procurement",
    "DEP12": "Administrative",
    "DEP13": "Legal",
    "DEP14": "Communications",
    "DEP15": "Partnerships",
}

# Job-title pattern per grade: {a} = work area, {d} = department, {r} = directorate
TITLE_PATTERN = {
    "G3": "{a} Assistant",
    "G4": "{a} Assistant",
    "G5": "Senior {a} Assistant",
    "G6": "Senior {a} Assistant",
    "G7": "{a} Associate",
    "P1": "Associate {a} Officer",
    "P2": "Associate {a} Officer",
    "P3": "{a} Officer",
    "P4": "Senior {a} Officer",
    "P5": "Chief, {d}",
    "D1": "Deputy Director, {r}",
    "D2": "Director, {r}",
}

# Consultants and interns are non-staff personnel: they hold no grade
NON_STAFF_TYPES = {"Consultant", "Intern"}
NON_STAFF_GRADE = "Non-staff"

# Realistic age range at hire, by grade band
AGE_AT_HIRE = {
    "G": (22, 50),
    "P1": (24, 35), "P2": (25, 40), "P3": (28, 48), "P4": (32, 52), "P5": (36, 55),
    "D": (42, 57),
    "Consultant": (26, 60),
    "Intern": (21, 28),
}

# Earliest hire date (days before the reporting period) for short-term types
EARLIEST_HIRE_DAYS = {"Intern": 180, "Temporary": 730, "Consultant": 1095}
CAREER_START = date(2008, 1, 1)       # earliest hire for long-term staff
SHARE_HIRED_DURING_PERIOD = 0.20      # long-term staff hired inside the 24 months
MAX_AGE_AT_PERIOD_START = 62


def weighted_choice(rng, weights: dict):
    """Pick one key from a {value: weight} dict."""
    keys = list(weights)
    p = np.array(list(weights.values()), dtype=float)
    return keys[rng.choice(len(keys), p=p / p.sum())]


def random_date(rng, start: date, end: date) -> date:
    """Uniform random date between start and end (inclusive)."""
    return start + timedelta(days=int(rng.integers(0, (end - start).days + 1)))


def age_band(grade: str, emp_type: str) -> str:
    if emp_type in NON_STAFF_TYPES:
        return emp_type
    if grade.startswith("G"):
        return "G"
    if grade.startswith("D"):
        return "D"
    return grade


def pick_nationality(rng, grade, emp_type, office_country, all_nationalities):
    """Local staff are mostly nationals; professionals are mostly international."""
    if emp_type in NON_STAFF_TYPES:
        p_local = 0.60
    elif grade.startswith("G"):
        p_local = 0.92
    else:
        p_local = 0.25
    if rng.random() < p_local:
        return office_country
    others = [n for n in all_nationalities if n != office_country]
    return others[rng.integers(len(others))]


def pick_name(rng, gender, nationality, names_cfg):
    pool = names_cfg["pools"][names_cfg["nationality_to_pool"][nationality]]
    if gender == "Female":
        firsts = pool["female"]
    elif gender == "Male":
        firsts = pool["male"]
    else:
        firsts = pool["female"] + pool["male"]
    return firsts[rng.integers(len(firsts))], pool["last"][rng.integers(len(pool["last"]))]


def pick_hire_date(rng, emp_type, period_start, period_end):
    if emp_type in EARLIEST_HIRE_DAYS:
        earliest = period_start - timedelta(days=EARLIEST_HIRE_DAYS[emp_type])
        return random_date(rng, earliest, period_end)
    if rng.random() < SHARE_HIRED_DURING_PERIOD:
        return random_date(rng, period_start, period_end)
    return random_date(rng, CAREER_START, period_start - timedelta(days=1))


def pick_date_of_birth(rng, band, hire_date, period_start):
    low, high = AGE_AT_HIRE[band]
    age = int(rng.integers(low, high + 1))
    # Long-serving staff must not be implausibly old at the start of the period
    years_before_start = max(0, (period_start - hire_date).days // 365)
    age = max(low, min(age, MAX_AGE_AT_PERIOD_START - years_before_start))
    birthday_this_year = hire_date - timedelta(days=int(age * 365.25))
    return birthday_this_year - timedelta(days=int(rng.integers(0, 365)))


def build_employees(cfg: dict, names_cfg: dict) -> pd.DataFrame:
    gen = cfg["generation"]
    rng = np.random.default_rng(gen["random_seed"])

    period_start = date.fromisoformat(gen["first_reporting_month"] + "-01")
    last_month_start = date.fromisoformat(gen["last_reporting_month"] + "-01")
    period_end = (pd.Timestamp(last_month_start) + pd.offsets.MonthEnd(0)).date()

    offices = {o["office_id"]: o for o in cfg["offices"]}
    office_weights = {o["office_id"]: o["headcount_weight"] for o in cfg["offices"]}

    departments = build_departments(cfg).set_index("department_id")
    # Programme departments are larger than support departments
    dept_weights = {
        d: (2 if row["department_category"] == "Programme" else 1)
        for d, row in departments.iterrows()
    }

    all_nationalities = list(names_cfg["nationality_to_pool"])

    rows = []
    for i in range(1, gen["total_employees"] + 1):
        office_id = weighted_choice(rng, office_weights)
        office = offices[office_id]
        dept_id = weighted_choice(rng, dept_weights)
        dept = departments.loc[dept_id]

        emp_type = weighted_choice(rng, cfg["employment_types"])
        grade = NON_STAFF_GRADE if emp_type in NON_STAFF_TYPES else weighted_choice(rng, cfg["grades"])

        gender = weighted_choice(rng, cfg["genders"])
        nationality = pick_nationality(rng, grade, emp_type, office["country"], all_nationalities)
        first_name, last_name = pick_name(rng, gender, nationality, names_cfg)

        hire_date = pick_hire_date(rng, emp_type, period_start, period_end)
        dob = pick_date_of_birth(rng, age_band(grade, emp_type), hire_date, period_start)

        area = JOB_AREA[dept_id]
        if emp_type in NON_STAFF_TYPES:
            title = f"{area} {emp_type}"
        else:
            title = TITLE_PATTERN[grade].format(
                a=area, d=dept["department_name"], r=dept["directorate"]
            )

        rows.append({
            "employee_id": f"E{i:05d}",
            "first_name": first_name,
            "last_name": last_name,
            "gender": gender,
            "date_of_birth": dob,
            "nationality": nationality,
            "office_id": office_id,
            "country_of_assignment": office["country"],
            "duty_station": office["city"],
            "department_id": dept_id,
            "job_title": title,
            "employment_type": emp_type,
            "grade_level": grade,
            "hire_date": hire_date,
        })

    return pd.DataFrame(rows)


def main() -> None:
    cfg = load_yaml("organisation.yaml")
    names_cfg = load_yaml("name_pools.yaml")
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)

    df = build_employees(cfg, names_cfg)
    out = TRUTH_DIR / "employees_base.csv"
    df.to_csv(out, index=False, encoding="utf-8")

    period_start = pd.Timestamp(cfg["generation"]["first_reporting_month"] + "-01")
    hire = pd.to_datetime(df["hire_date"])
    age_at_hire = (hire - pd.to_datetime(df["date_of_birth"])).dt.days // 365

    print(f"Employees: {len(df)} rows -> {out}\n")
    print("By office:")
    print(df["office_id"].value_counts().sort_index().to_string(), "\n")
    print("By employment type:")
    print(df["employment_type"].value_counts().to_string(), "\n")
    print(f"Hired before period start: {(hire < period_start).sum()}")
    print(f"Hired during period:       {(hire >= period_start).sum()}")
    print(f"Age range at hire:         {age_at_hire.min()} to {age_at_hire.max()}")
    print(f"Unique employee IDs:       {df['employee_id'].is_unique}\n")
    print(df.sample(5, random_state=1)[
        ["employee_id", "first_name", "last_name", "nationality", "duty_station", "job_title"]
    ].to_string(index=False))


if __name__ == "__main__":
    main()