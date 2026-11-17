"""
Simulate employee movements month by month across the reporting period.

Starting from each employee's state at hire (employees_base.csv), we walk
through every month and apply HR events in a fixed order:

  1. Contract end   -> 'Contract renewal' or 'Contract expiry' (separation)
  2. Separations    -> 'Retirement', 'Resignation', 'Termination'
  3. Career events  -> 'Promotion', 'Transfer'

Outputs (data/synthetic/truth/):
  movements.csv        one row per movement event
  employees_final.csv  each employee's state at the end of the period
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from src.config import load_yaml
from src.generation.employees import JOB_AREA, NON_STAFF_TYPES, TITLE_PATTERN
from src.generation.reference_data import TRUTH_DIR, build_departments

# ---- Contract rules ------------------------------------------------------
# Contract length in months: (min, max). Continuing contracts have no end date.
CONTRACT_MONTHS = {"Fixed-term": (24, 24), "Temporary": (6, 12), "Consultant": (6, 12), "Intern": (3, 6)}
RENEWAL_PROBABILITY = {"Fixed-term": 0.88, "Temporary": 0.50, "Consultant": 0.60, "Intern": 0.0}
MAX_TEMPORARY_MONTHS = 24          # temporary appointments cannot run longer than this

# ---- Monthly event probabilities (annual rate / 12) -------------------------
P_RESIGN = 0.06 / 12
P_TERMINATE = 0.01 / 12
P_EARLY_RETIRE = 0.18 / 12         # applies from age 60
MANDATORY_RETIREMENT_AGE = 65
P_PROMOTE = 0.08 / 12              # staff only, after 12 months in grade
P_TRANSFER = 0.04 / 12             # staff only

GRADE_LADDER = ["G3", "G4", "G5", "G6", "G7", "P1", "P2", "P3", "P4", "P5", "D1", "D2"]
GRADE_CEILING = {"G7", "D2"}       # G staff cannot be promoted into P grades


def add_months(d: date, months: int) -> date:
    return (pd.Timestamp(d) + pd.DateOffset(months=months)).date()


def month_starts(first: str, last: str) -> list[date]:
    return [p.date() for p in pd.date_range(first + "-01", last + "-01", freq="MS")]


def random_day_in_month(rng, month_start: date) -> date:
    month_end = add_months(month_start, 1) - timedelta(days=1)
    return month_start + timedelta(days=int(rng.integers(0, (month_end - month_start).days + 1)))


def age_on(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def make_title(grade, emp_type, dept_id, departments):
    area = JOB_AREA[dept_id]
    if emp_type in NON_STAFF_TYPES:
        return f"{area} {emp_type}"
    dept = departments.loc[dept_id]
    return TITLE_PATTERN[grade].format(a=area, d=dept["department_name"], r=dept["directorate"])


def initial_contract(rng, emp_type, hire_date, period_start):
    """Return (contract_start, contract_end) in force at max(hire, period start).

    Contracts for people hired before the period are rolled forward from the
    hire date, so the contract in force on day one of the period is realistic.
    """
    if emp_type == "Continuing":
        return hire_date, None
    lo, hi = CONTRACT_MONTHS[emp_type]
    start = hire_date
    end = add_months(start, int(rng.integers(lo, hi + 1))) - timedelta(days=1)
    while end < period_start:
        start = end + timedelta(days=1)
        end = add_months(start, int(rng.integers(lo, hi + 1))) - timedelta(days=1)
    return start, end


def simulate(cfg: dict, base: pd.DataFrame):
    gen = cfg["generation"]
    # A separate random stream from the employee generator: changing one
    # generator does not silently change the other's output.
    rng = np.random.default_rng([gen["random_seed"], 1])

    months = month_starts(gen["first_reporting_month"], gen["last_reporting_month"])
    period_start, period_end = months[0], add_months(months[-1], 1) - timedelta(days=1)

    departments = build_departments(cfg).set_index("department_id")
    dept_ids = list(departments.index)
    offices = {o["office_id"]: o for o in cfg["offices"]}
    office_ids = list(offices)

    movements, finals = [], []

    for emp in base.itertuples(index=False):
        hire = date.fromisoformat(str(emp.hire_date))
        dob = date.fromisoformat(str(emp.date_of_birth))
        s = {  # current state of this employee
            "department_id": emp.department_id, "office_id": emp.office_id,
            "grade_level": emp.grade_level, "employment_type": emp.employment_type,
            "job_title": emp.job_title, "status": "Active", "exit_date": None,
            "last_change": hire,
        }
        s["contract_start"], s["contract_end"] = initial_contract(rng, emp.employment_type, hire, period_start)

        def record(mtype, when, prev_dept=None, prev_loc=None, prev_grade=None):
            movements.append({
                "employee_id": emp.employee_id, "movement_type": mtype,
                "previous_department": prev_dept or s["department_id"], "new_department": s["department_id"],
                "previous_location": prev_loc or s["office_id"], "new_location": s["office_id"],
                "previous_grade": prev_grade or s["grade_level"], "new_grade": s["grade_level"],
                "effective_date": when,
            })

        def separate(mtype, when):
            s["status"], s["exit_date"], s["last_change"] = "Separated", when, when
            record(mtype, when)

        for m in months:
            if s["status"] != "Active":
                break
            if hire > add_months(m, 1) - timedelta(days=1):
                continue  # not hired yet
            month_end = add_months(m, 1) - timedelta(days=1)
            earliest_event = max(m, hire + timedelta(days=1))
            if earliest_event > month_end:
                continue  # hired on the last day of the month

            # 1. Contract end falls in this month -> renew or let it expire
            ce = s["contract_end"]
            if ce is not None and m <= ce <= month_end:
                etype = s["employment_type"]
                months_served = (ce - hire).days / 30.44
                can_renew = not (etype == "Temporary" and months_served >= MAX_TEMPORARY_MONTHS)
                if can_renew and rng.random() < RENEWAL_PROBABILITY[etype]:
                    lo, hi = CONTRACT_MONTHS[etype]
                    s["contract_start"] = ce + timedelta(days=1)
                    s["contract_end"] = add_months(s["contract_start"], int(rng.integers(lo, hi + 1))) - timedelta(days=1)
                    s["last_change"] = s["contract_start"]
                    if s["contract_start"] <= period_end:
                        record("Contract renewal", s["contract_start"])
                    # Nothing else may happen this month before the renewal date
                    earliest_event = max(earliest_event, s["contract_start"])
                    if earliest_event > month_end:
                        continue
                else:
                    separate("Contract expiry", ce)
                    continue

            # 2. Separations
            when = random_day_in_month(rng, earliest_event)
            when = max(when, earliest_event)
            age = age_on(dob, month_end)
            if age >= MANDATORY_RETIREMENT_AGE:
                separate("Retirement", when)
                continue
            if age >= 60 and rng.random() < P_EARLY_RETIRE:
                separate("Retirement", when)
                continue
            if rng.random() < P_RESIGN:
                separate("Resignation", when)
                continue
            if rng.random() < P_TERMINATE:
                separate("Termination", when)
                continue

            # 3. Career events (staff only)
            if s["employment_type"] in NON_STAFF_TYPES:
                continue
            months_since_change = (when - s["last_change"]).days / 30.44
            grade = s["grade_level"]
            if grade not in GRADE_CEILING and months_since_change >= 12 and rng.random() < P_PROMOTE:
                prev = grade
                s["grade_level"] = GRADE_LADDER[GRADE_LADDER.index(grade) + 1]
                s["job_title"] = make_title(s["grade_level"], s["employment_type"], s["department_id"], departments)
                s["last_change"] = when
                record("Promotion", when, prev_grade=prev)
            elif months_since_change >= 12 and rng.random() < P_TRANSFER:
                prev_dept, prev_loc = s["department_id"], s["office_id"]
                if grade.startswith("G"):
                    # Local staff: move department within the same duty station
                    s["department_id"] = rng.choice([d for d in dept_ids if d != prev_dept])
                else:
                    # International staff: move duty station, sometimes department too
                    s["office_id"] = rng.choice([o for o in office_ids if o != prev_loc])
                    if rng.random() < 0.3:
                        s["department_id"] = rng.choice([d for d in dept_ids if d != prev_dept])
                s["job_title"] = make_title(grade, s["employment_type"], s["department_id"], departments)
                s["last_change"] = when
                record("Transfer", when, prev_dept=prev_dept, prev_loc=prev_loc)

        office = offices[s["office_id"]]
        finals.append({
            **emp._asdict(),
            "office_id": s["office_id"],
            "country_of_assignment": office["country"],
            "duty_station": office["city"],
            "department_id": s["department_id"],
            "job_title": s["job_title"],
            "grade_level": s["grade_level"],
            "contract_start_date": s["contract_start"],
            "contract_end_date": s["contract_end"],
            "employment_status": s["status"],
            "exit_date": s["exit_date"],
            "last_updated": min(s["last_change"], period_end),
        })

    mv = pd.DataFrame(movements).sort_values(["effective_date", "employee_id"]).reset_index(drop=True)
    mv.insert(0, "movement_id", [f"M{i:06d}" for i in range(1, len(mv) + 1)])
    return mv, pd.DataFrame(finals)


def main() -> None:
    cfg = load_yaml("organisation.yaml")
    base = pd.read_csv(TRUTH_DIR / "employees_base.csv", dtype=str)

    movements, final = simulate(cfg, base)
    movements.to_csv(TRUTH_DIR / "movements.csv", index=False, encoding="utf-8")
    final.to_csv(TRUTH_DIR / "employees_final.csv", index=False, encoding="utf-8")

    print(f"Movements: {len(movements)} rows -> {TRUTH_DIR / 'movements.csv'}")
    print(f"Final employee states: {len(final)} rows -> {TRUTH_DIR / 'employees_final.csv'}\n")
    print("Movements by type:")
    print(movements["movement_type"].value_counts().to_string(), "\n")
    print("Employment status at end of period:")
    print(final["employment_status"].value_counts().to_string(), "\n")

    # Sanity checks: these must all be True
    eff = pd.to_datetime(movements["effective_date"])
    hire = pd.to_datetime(movements["employee_id"].map(base.set_index("employee_id")["hire_date"]))
    exits = movements[movements["movement_type"].isin(["Resignation", "Retirement", "Termination", "Contract expiry"])]
    print("Checks:")
    print(f"  No movement before hire date:       {(eff >= hire).all()}")
    print(f"  At most one exit per employee:      {exits['employee_id'].is_unique}")
    print(f"  Exits match 'Separated' count:      {len(exits) == (final['employment_status'] == 'Separated').sum()}")
    exit_dates = pd.to_datetime(exits.set_index("employee_id")["effective_date"])
    after_exit = eff > movements["employee_id"].map(exit_dates)   # NaT (no exit) compares False
    print(f"  No movement after exit date:        {not after_exit.any()}")
    print(f"  Movement IDs unique:                {movements['movement_id'].is_unique}")


if __name__ == "__main__":
    main()