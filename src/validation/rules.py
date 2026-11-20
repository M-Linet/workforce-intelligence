"""
The rule catalogue: every check the pipeline performs, in one place.

Each rule has:
  dimension : which data-quality dimension it measures (the 5 in the brief)
  severity  : info < warning < error < critical
  action    : what happens to the record
                corrected     -> fixed by a documented rule, record is loaded
                flagged       -> loaded, but the bad field is set to NULL and reported
                manual_review -> held back for a person to decide (not loaded)
                rejected      -> cannot be used (not loaded)

A record's CATEGORY is decided by its most serious action:
  no issues                  -> 'valid'
  only corrected / flagged   -> 'corrected'      (requiring correction: done automatically)
  any manual_review          -> 'manual_review'
  any rejected               -> 'rejected'
"""

from dataclasses import dataclass, field

RULES = {
    # rule_id                 (dimension,               severity,   action,          description)
    "WHITESPACE":             ("consistency",           "info",     "corrected",     "Leading, trailing or repeated spaces removed"),
    "CATEGORY_VARIANT":       ("consistency",           "info",     "corrected",     "Category written differently (e.g. 'F', 'Resigned') mapped to the standard value"),
    "CATEGORY_UNKNOWN":       ("validity",              "error",    "manual_review", "Category value not recognised"),
    "COUNTRY_VARIANT":        ("consistency",           "info",     "corrected",     "Country name variant mapped to the standard name"),
    "COUNTRY_UNKNOWN":        ("validity",              "warning",  "flagged",       "Country name not recognised"),
    "COUNTRY_MISMATCH":       ("consistency",           "warning",  "flagged",       "Country of assignment differs from the office's country"),
    "DATE_FORMAT_VARIANT":    ("consistency",           "warning",  "corrected",     "Date not in the office's registered format; parsed with an alternative"),
    "DATE_INVALID":           ("validity",              "error",    "manual_review", "Required date cannot be interpreted (e.g. 30 February)"),
    "DATE_INVALID_OPTIONAL":  ("validity",              "warning",  "flagged",       "Optional date cannot be interpreted; set to NULL"),
    "DATE_SEQUENCE":          ("consistency",           "error",    "manual_review", "Dates in impossible order (e.g. contract ends before it starts)"),
    "DOB_AFTER_HIRE":         ("consistency",           "warning",  "flagged",       "Date of birth not before hire date; date of birth set to NULL"),
    "PRIMARY_KEY_MISSING":    ("completeness",          "critical", "rejected",      "Record identifier is missing"),
    "ID_FORMAT":              ("validity",              "critical", "rejected",      "Identifier does not match the expected pattern"),
    "REQUIRED_MISSING":       ("completeness",          "error",    "manual_review", "A value needed for reporting is missing"),
    "OPTIONAL_MISSING":       ("completeness",          "warning",  "flagged",       "A value is missing; record loaded without it"),
    "EMPLOYMENT_TYPE_INFERRED": ("completeness",        "warning",  "corrected",     "Employment type missing; inferred from job title and contract dates"),
    "DEPARTMENT_NAME_AS_CODE": ("consistency",          "info",     "corrected",     "Department name given instead of its code; code looked up"),
    "REF_DEPARTMENT":         ("referential_integrity", "warning",  "flagged",       "Department code does not exist; department set to NULL (unassigned)"),
    "REF_OFFICE":             ("referential_integrity", "error",    "manual_review", "Office code does not exist"),
    "REF_EMPLOYEE":           ("referential_integrity", "error",    "manual_review", "Employee not found in the accepted employee master"),
    "REF_SUPERVISOR":         ("referential_integrity", "warning",  "flagged",       "Supervisor not found in the accepted employee master; set to NULL"),
    "GRADE_FORMAT":           ("consistency",           "info",     "corrected",     "Grade written differently (e.g. 'p3', 'P-3') normalised"),
    "GRADE_INVALID":          ("validity",              "error",    "manual_review", "Grade does not exist in the grade structure"),
    "MONTH_MISMATCH":         ("consistency",           "warning",  "manual_review", "Reporting month in the record differs from the file's month"),
    "NUMBER_INVALID":         ("validity",              "error",    "manual_review", "Count is not a whole non-negative number (e.g. 'N/A', -12)"),
    "FUNNEL_INCONSISTENT":    ("consistency",           "error",    "manual_review", "Recruitment funnel widens (e.g. more shortlisted than applied)"),
    "OUTLIER_APPLICATIONS":   ("validity",              "warning",  "manual_review", "Application count implausibly high"),
    "DUPLICATE_RECORD":       ("uniqueness",            "warning",  "rejected",      "Same record submitted more than once; first copy kept"),
    "STALE_DUPLICATE":        ("uniqueness",            "warning",  "rejected",      "Older version of an employee record; most recent version kept"),
    "EXIT_DATE_ESTIMATED":    ("completeness",          "warning",  "corrected",     "Separated employee with no exit movement; exit date taken from last_updated"),
    # File-level rules (recorded by the extract stage)
    "FILE_EMPTY":             ("completeness",          "critical", "rejected",      "File is 0 bytes"),
    "FILE_UNREADABLE":        ("validity",              "critical", "rejected",      "File cannot be read as a table"),
    "FILE_DUPLICATE":         ("uniqueness",            "info",     "rejected",      "File content identical to a file already received"),
}

ACTION_RANK = {"corrected": 1, "flagged": 1, "manual_review": 2, "rejected": 3}
CATEGORY_BY_RANK = {0: "valid", 1: "corrected", 2: "manual_review", 3: "rejected"}


@dataclass
class RecordCheck:
    """Collects the issues found in ONE staged record."""
    dataset: str
    file_id: int
    source_row: int
    office_id: str | None
    record_key: str | None
    issues: list = field(default_factory=list)

    def add(self, rule_id: str, column: str | None, original=None, corrected=None):
        dimension, severity, action, _ = RULES[rule_id]
        self.issues.append({
            "file_id": self.file_id, "office_id": self.office_id, "dataset": self.dataset,
            "record_key": self.record_key, "column_name": column, "rule_id": rule_id,
            "dimension": dimension, "severity": severity, "action_taken": action,
            "original_value": None if original is None else str(original),
            "corrected_value": None if corrected is None else str(corrected),
        })

    @property
    def category(self) -> str:
        rank = max((ACTION_RANK[i["action_taken"]] for i in self.issues), default=0)
        return CATEGORY_BY_RANK[rank]

    @property
    def reasons(self) -> list:
        return sorted({i["rule_id"] for i in self.issues if ACTION_RANK[i["action_taken"]] >= 2})
