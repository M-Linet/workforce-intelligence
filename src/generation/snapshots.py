"""
Build monthly workforce snapshots by replaying movements.

For every reporting month we record where each employee was at MONTH END:
their department, office, grade and status.

Who appears in a month's snapshot?
  - 'Active'    : hired on/before month end and not yet separated
  - 'Separated' : separated DURING that month (so exits can be counted per month)
Employees who left in an earlier month do not appear again.

Grain: one row per employee per reporting month.
Output: data/synthetic/truth/snapshots.csv
"""

from datetime import date, timedelta

import pandas as pd

from src.config import load_yaml
from src.generation.movements import add_months, month_starts
from src.generation.reference_data import TRUTH_DIR

EXIT_TYPES = {"Resignation", "Retirement", "Termination", "Contract expiry"}


def build_snapshots(cfg: dict, base: pd.DataFrame, movements: pd.DataFrame) -> pd.DataFrame:
    gen = cfg["generation"]
    months = month_starts(gen["first_reporting_month"], gen["last_reporting_month"])

    # Each employee's movements, in date order
    mv = movements.sort_values(["effective_date", "movement_id"])
    by_employee = {eid: g.to_dict("records") for eid, g in mv.groupby("employee_id")}

    rows = []
    for emp in base.itertuples(index=False):
        hire = date.fromisoformat(emp.hire_date)
        state = {"department_id": emp.department_id, "office_id": emp.office_id,
                 "grade_level": emp.grade_level}
        events = by_employee.get(emp.employee_id, [])
        i = 0
        for m in months:
            month_end = add_months(m, 1) - timedelta(days=1)
            if hire > month_end:
                continue                      # not yet hired

            # Apply every movement effective on or before month end
            separated_this_month = False
            while i < len(events) and date.fromisoformat(events[i]["effective_date"]) <= month_end:
                ev = events[i]
                state = {"department_id": ev["new_department"], "office_id": ev["new_location"],
                         "grade_level": ev["new_grade"]}
                if ev["movement_type"] in EXIT_TYPES:
                    separated_this_month = date.fromisoformat(ev["effective_date"]) >= m
                    state["exited"] = True
                i += 1

            if state.get("exited") and not separated_this_month:
                break                         # left in an earlier month

            rows.append({
                "employee_id": emp.employee_id,
                "reporting_month": m.strftime("%Y-%m"),
                "employment_status": "Separated" if state.get("exited") else "Active",
                "department_id": state["department_id"],
                "office_id": state["office_id"],
                "grade_level": state["grade_level"],
            })

    snaps = pd.DataFrame(rows).sort_values(["reporting_month", "employee_id"]).reset_index(drop=True)
    snaps.insert(0, "snapshot_id", [f"S{i:07d}" for i in range(1, len(snaps) + 1)])
    return snaps


def main() -> None:
    cfg = load_yaml("organisation.yaml")
    base = pd.read_csv(TRUTH_DIR / "employees_base.csv", dtype=str)
    movements = pd.read_csv(TRUTH_DIR / "movements.csv", dtype=str)
    final = pd.read_csv(TRUTH_DIR / "employees_final.csv", dtype=str)

    snaps = build_snapshots(cfg, base, movements)
    out = TRUTH_DIR / "snapshots.csv"
    snaps.to_csv(out, index=False, encoding="utf-8")

    active = snaps[snaps["employment_status"] == "Active"]
    headcount = active.groupby("reporting_month").size()
    exits = snaps[snaps["employment_status"] == "Separated"].groupby("reporting_month").size()

    print(f"Snapshots: {len(snaps)} rows -> {out}")
    print(f"Months covered: {snaps['reporting_month'].nunique()} "
          f"({snaps['reporting_month'].min()} to {snaps['reporting_month'].max()})\n")
    print("Month-end headcount (first 3 and last 3 months):")
    print(pd.concat([headcount.head(3), headcount.tail(3)]).to_string(), "\n")

    # Reconciliation checks: snapshots must agree with the other truth tables
    last_month = snaps["reporting_month"].max()
    n_exits_mv = movements["movement_type"].isin(EXIT_TYPES).sum()
    n_active_final = (final["employment_status"] == "Active").sum()
    print("Checks:")
    print(f"  One row per employee per month:      {not snaps.duplicated(['employee_id', 'reporting_month']).any()}")
    print(f"  Last-month headcount = final Active: {headcount[last_month] == n_active_final}  ({headcount[last_month]} vs {n_active_final})")
    print(f"  Exits in snapshots = exit movements: {exits.sum() == n_exits_mv}  ({exits.sum()} vs {n_exits_mv})")


if __name__ == "__main__":
    main()