"""
Evaluate the pipeline against the ANSWER KEY (the corruption manifest).

This is the only code that reads data/synthetic/ after generation: the pipeline
itself never sees the manifest or the clean "true world".

Two questions:
  1. DETECTION: of the problems we injected, how many did the pipeline flag?
  2. RECONCILIATION: how close is the loaded warehouse to the true data?

Usage:  python -m src.analytics.evaluate          (also writes reports/evaluation/*.csv)
"""

import pandas as pd
from sqlalchemy import text

from src.config import PROJECT_ROOT, SYNTHETIC_DIR
from src.database.connection import get_engine

OUT_DIR = PROJECT_ROOT / "reports" / "evaluation"
FILE_LEVEL = {"corrupted_file": "FILE_UNREADABLE", "zero_byte_file": "FILE_EMPTY", "resubmitted_file": "FILE_DUPLICATE"}


def load(engine):
    manifest = pd.read_csv(SYNTHETIC_DIR / "corruption_manifest.csv", dtype=str)
    with engine.connect() as conn:
        issues = pd.read_sql(text("""
            SELECT q.dataset, q.record_key, q.rule_id, q.office_id, r.reporting_month AS month, r.status AS file_status
            FROM audit.data_quality_issues q LEFT JOIN audit.etl_file_registry r ON r.file_id = q.file_id"""), conn)
        lost = pd.read_sql(text("""
            SELECT office_id, dataset, reporting_month AS month FROM audit.etl_file_registry
            WHERE status IN ('unreadable') OR (status = 'empty' AND file_size_bytes = 0)"""), conn)
    return manifest, issues, lost


def detection(manifest: pd.DataFrame, issues: pd.DataFrame, lost: pd.DataFrame) -> pd.DataFrame:
    lost_files = set(map(tuple, lost[["office_id", "dataset", "month"]].values))
    flagged_keys = set(map(tuple, issues.dropna(subset=["record_key"])[["dataset", "record_key"]].values))
    file_issues = set(map(tuple, issues[issues["rule_id"].str.startswith("FILE")][["office_id", "dataset", "month", "rule_id"]].values))
    # Records whose ID was blanked can only be matched by file
    pk_missing = issues[issues["rule_id"] == "PRIMARY_KEY_MISSING"].groupby(["office_id", "dataset", "month"]).size().to_dict()

    outcomes = []
    for m in manifest.itertuples(index=False):
        if m.issue_type in FILE_LEVEL:
            hit = (m.office_id, m.dataset, m.month, FILE_LEVEL[m.issue_type]) in file_issues
            outcomes.append("detected" if hit else "missed")
        elif (m.office_id, m.dataset, m.month) in lost_files:
            outcomes.append("in unusable file")
        elif m.issue_type == "missing_primary_key" and m.dataset == "employee_master":
            key = (m.office_id, m.dataset, m.month)
            outcomes.append("detected" if pk_missing.get(key, 0) > 0 else "missed")
            if pk_missing.get(key, 0) > 0:
                pk_missing[key] -= 1
        else:
            outcomes.append("detected" if (m.dataset, m.record_key) in flagged_keys else "missed")
    manifest = manifest.assign(outcome=outcomes)

    summary = (manifest.groupby("issue_type")["outcome"].value_counts().unstack(fill_value=0)
               .reindex(columns=["detected", "missed", "in unusable file"], fill_value=0))
    summary["injected"] = summary.sum(axis=1)
    checkable = summary["detected"] + summary["missed"]
    summary["detection_rate_pct"] = (100 * summary["detected"] / checkable.where(checkable > 0)).round(1)
    return manifest, summary.sort_values("injected", ascending=False)


def reconciliation(engine) -> pd.DataFrame:
    truth = pd.read_csv(SYNTHETIC_DIR / "truth" / "snapshots.csv", dtype=str)
    true_hc = truth[truth["employment_status"] == "Active"].groupby("reporting_month").size().rename("true_headcount")
    with engine.connect() as conn:
        loaded = pd.read_sql(text("SELECT reporting_month, SUM(headcount) AS loaded_headcount "
                                  "FROM dw.fact_workforce_snapshot GROUP BY 1"), conn).set_index("reporting_month")
    rec = pd.concat([true_hc, loaded["loaded_headcount"]], axis=1)
    rec["difference"] = rec["loaded_headcount"] - rec["true_headcount"]
    rec["pct_of_truth"] = (100 * rec["loaded_headcount"] / rec["true_headcount"]).round(1)
    return rec


def main():
    engine = get_engine()
    manifest, issues, lost = load(engine)
    detailed, summary = detection(manifest, issues, lost)
    rec = reconciliation(engine)

    total = summary[["detected", "missed", "in unusable file"]].sum()
    checkable = total["detected"] + total["missed"]
    print("DETECTION: injected problems found by the pipeline\n")
    print(summary.to_string())
    print(f"\nOverall: {total['detected']} of {checkable} checkable problems detected "
          f"({100 * total['detected'] / checkable:.1f}%); "
          f"{total['in unusable file']} were inside files that could not be read at all.")

    print("\nRECONCILIATION: month-end headcount, true data vs warehouse\n")
    print(rec.to_string())
    print(f"\nAverage: warehouse holds {rec['pct_of_truth'].mean():.1f}% of the true headcount. "
          f"Months below 95%: {', '.join(rec.index[rec['pct_of_truth'] < 95]) or 'none'}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUT_DIR / "detection_by_issue_type.csv")
    detailed[detailed["outcome"] == "missed"].to_csv(OUT_DIR / "missed_problems.csv", index=False)
    rec.to_csv(OUT_DIR / "headcount_reconciliation.csv")
    print(f"\nDetails saved to {OUT_DIR}")


if __name__ == "__main__":
    main()
