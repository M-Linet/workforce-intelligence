"""
Build the final Employee Master table, including supervisors.

Supervisor rules (applied to each employee's final unit = office + department):
  - Unit head   : highest-graded ACTIVE staff member in the unit
  - Office head : highest-graded ACTIVE staff member in the office
  - Org head    : highest-graded ACTIVE staff member at Headquarters

  Ordinary employee -> reports to their unit head
  Unit head         -> reports to the office head
  Office head       -> reports to the org head
  Org head          -> has no supervisor (the only blank supervisor_id)

Ties are broken by earliest hire date, then by employee_id, so the result is
deterministic. No randomness is used in this step.

Output: data/synthetic/truth/employees_master.csv
"""

import pandas as pd

from src.config import load_yaml
from src.generation.movements import GRADE_LADDER
from src.generation.reference_data import TRUTH_DIR

# Column order follows the project brief (Dataset 1: Employee Master Records)
MASTER_COLUMNS = [
    "employee_id", "first_name", "last_name", "gender", "date_of_birth", "nationality",
    "country_of_assignment", "duty_station", "department_id", "job_title", "employment_type",
    "grade_level", "contract_start_date", "contract_end_date", "employment_status",
    "supervisor_id", "hire_date", "source_office", "last_updated",
    "exit_date",  # kept in the ground truth; not part of what offices submit
]


def grade_rank(grade: str) -> int:
    """Higher number = more senior. Non-staff rank below every grade."""
    return GRADE_LADDER.index(grade) if grade in GRADE_LADDER else -1


def pick_head(candidates: pd.DataFrame):
    """Most senior candidate: highest grade, then earliest hire, then lowest ID."""
    if candidates.empty:
        return None
    ordered = candidates.sort_values(["rank", "hire_date", "employee_id"], ascending=[False, True, True])
    return ordered.iloc[0]["employee_id"]


def assign_supervisors(final: pd.DataFrame, hq_office: str) -> pd.Series:
    df = final.copy()
    df["rank"] = df["grade_level"].map(grade_rank)
    staff = df[(df["employment_status"] == "Active") & (df["rank"] >= 0)]

    org_head = pick_head(staff[staff["office_id"] == hq_office])
    office_head = {o: pick_head(g) for o, g in staff.groupby("office_id")}
    unit_head = {u: pick_head(g) for u, g in staff.groupby(["office_id", "department_id"])}

    def supervisor_for(row):
        eid, office = row["employee_id"], row["office_id"]
        if eid == org_head:
            return None
        if eid == office_head.get(office):
            return org_head
        head = unit_head.get((office, row["department_id"]))
        if head is None or head == eid:
            return office_head.get(office, org_head)
        return head

    return df.apply(supervisor_for, axis=1)


def build_master(cfg: dict, final: pd.DataFrame) -> pd.DataFrame:
    hq_office = next(o["office_id"] for o in cfg["offices"] if o["office_type"] == "Headquarters")
    master = final.copy()
    master["supervisor_id"] = assign_supervisors(master, hq_office)
    master["source_office"] = master["office_id"]
    return master[MASTER_COLUMNS]


def main() -> None:
    cfg = load_yaml("organisation.yaml")
    final = pd.read_csv(TRUTH_DIR / "employees_final.csv", dtype=str)

    master = build_master(cfg, final)
    out = TRUTH_DIR / "employees_master.csv"
    master.to_csv(out, index=False, encoding="utf-8")

    sup = master["supervisor_id"]
    active_ids = set(master.loc[master["employment_status"] == "Active", "employee_id"])
    span = sup.value_counts()

    # Walk up the reporting chain from every employee; it must reach the top within a few steps
    parent = dict(zip(master["employee_id"], sup))
    def chain_length(eid, limit=10):
        steps = 0
        while pd.notna(parent.get(eid)) and steps < limit:
            eid, steps = parent[eid], steps + 1
        return steps
    chains = master["employee_id"].map(chain_length)

    top = master[sup.isna()].iloc[0]
    print(f"Employee master: {len(master)} rows -> {out}\n")
    print(f"Head of organisation: {top['employee_id']} - {top['first_name']} {top['last_name']}, "
          f"{top['job_title']} ({top['grade_level']}, {top['duty_station']})")
    print(f"People with direct reports: {len(span)}")
    print(f"Largest team size:          {span.max()}")
    print(f"Longest reporting chain:    {chains.max()} levels\n")
    print("Checks:")
    print(f"  Exactly one person without a supervisor: {sup.isna().sum() == 1}")
    print(f"  Nobody supervises themselves:            {(sup != master['employee_id']).all()}")
    print(f"  Every supervisor is an Active employee:  {sup.dropna().isin(active_ids).all()}")
    print(f"  No reporting loops:                      {(chains < 10).all()}")


if __name__ == "__main__":
    main()