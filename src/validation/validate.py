"""
TRANSFORM + VALIDATE stage.

Reads every staged record, cleans it with the documented rules, records every
issue found, and classifies each record:

    valid / corrected  -> loaded into the data warehouse
    manual_review      -> held in audit.rejected_records for a person to decide
    rejected           -> held in audit.rejected_records, not usable

Order matters, because later datasets are checked against earlier ones:
    1. departments, offices    (reference data)
    2. employee_master         (which employees are accepted)
    3. movements               (checked against accepted employees; exits give exit dates)
    4. workforce_snapshot      (checked against accepted employees)
    5. recruitment
"""

from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import text

from src.config import load_yaml
from src.transformation import standardise as st
from src.validation.rules import RecordCheck

EXIT_TYPES = {"Contract expiry", "Resignation", "Retirement", "Termination"}
ID_PATTERNS = {"employee_id": r"^E\d{5}$", "department_id": r"^DEP\d{2}$", "office_id": r"^OFF\d{2}$"}


@dataclass
class DatasetResult:
    dataset: str
    loadable: pd.DataFrame
    held: list = field(default_factory=list)       # rows for audit.rejected_records
    issues: list = field(default_factory=list)     # rows for audit.data_quality_issues
    categories: dict = field(default_factory=dict)  # category -> count


class Validator:
    def __init__(self, engine):
        self.engine = engine
        self.cfg = load_yaml("standardisation.yaml")
        self.lookups = {k: st.build_lookup(v) for k, v in self.cfg["value_maps"].items()}
        self.countries = st.build_lookup({**{c: [] for c in self.cfg["valid_countries"]},
                                          **self.cfg["country_aliases"]})
        self.valid_grades = set(self.cfg["valid_grades"])
        self.files = self._file_metadata()
        self.departments = {}        # department_id -> department_id
        self.department_names = {}   # casefolded name -> department_id
        self.offices = {}            # office_id -> country
        self.accepted_employees = set()

    def _file_metadata(self) -> dict:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT file_id, office_id, reporting_month FROM audit.etl_file_registry WHERE status = 'received'"))
            return {r.file_id: (r.office_id, r.reporting_month) for r in rows}

    def staged(self, dataset: str) -> pd.DataFrame:
        with self.engine.connect() as conn:
            df = pd.read_sql(text(f"SELECT * FROM staging.{dataset} ORDER BY file_id, source_row"), conn)
        return df.drop(columns=["staged_at"])

    # -- helpers that clean one field and record what happened -----------------
    def text_field(self, chk, row, col, required=False):
        value, outcome = st.clean_text(row[col])
        if outcome == st.CORRECTED:
            chk.add("WHITESPACE", col, row[col], value)
        elif outcome == st.MISSING:
            chk.add("REQUIRED_MISSING" if required else "OPTIONAL_MISSING", col)
        return value

    def category_field(self, chk, row, col, vocab, required=True):
        value, outcome = st.map_category(row[col], self.lookups[vocab])
        if outcome == st.CORRECTED:
            chk.add("CATEGORY_VARIANT", col, row[col], value)
        elif outcome == st.INVALID:
            chk.add("CATEGORY_UNKNOWN", col, row[col])
            return None
        elif outcome == st.MISSING:
            chk.add("REQUIRED_MISSING" if required else "OPTIONAL_MISSING", col)
        return value

    def date_field(self, chk, row, col, required=False):
        office = chk.office_id
        fmt = self.cfg["office_date_formats"].get(office, "%Y-%m-%d")
        value, outcome = st.parse_date(row[col], fmt, self.cfg["fallback_date_formats"],
                                       self.cfg["swapped_date_formats"])
        if outcome == st.CORRECTED:
            chk.add("DATE_FORMAT_VARIANT", col, row[col], value)
        elif outcome == st.INVALID:
            chk.add("DATE_INVALID" if required else "DATE_INVALID_OPTIONAL", col, row[col])
            return None
        elif outcome == st.MISSING and required:
            chk.add("REQUIRED_MISSING", col)
        return value

    def grade_field(self, chk, row, col, required=True):
        value, outcome = st.normalise_grade(row[col], self.valid_grades)
        if outcome == st.CORRECTED:
            chk.add("GRADE_FORMAT", col, row[col], value)
        elif outcome == st.INVALID:
            chk.add("GRADE_INVALID" if required else "OPTIONAL_MISSING", col, row[col])
            return None
        elif outcome == st.MISSING and required:
            chk.add("REQUIRED_MISSING", col)
        return value

    def department_field(self, chk, row, col):
        raw = row[col]
        if st.is_blank(raw):
            chk.add("OPTIONAL_MISSING", col)
            return None
        value = str(raw).strip()
        if value in self.departments:
            return value
        by_name = self.department_names.get(" ".join(value.split()).casefold())
        if by_name:
            chk.add("DEPARTMENT_NAME_AS_CODE", col, raw, by_name)
            return by_name
        chk.add("REF_DEPARTMENT", col, raw)
        return None

    def office_field(self, chk, row, col):
        value = None if st.is_blank(row[col]) else str(row[col]).strip()
        if value not in self.offices:
            chk.add("REF_OFFICE", col, row[col])
            return None
        return value

    def employee_ref(self, chk, row, col="employee_id"):
        value = None if st.is_blank(row[col]) else str(row[col]).strip()
        if value is None:
            chk.add("PRIMARY_KEY_MISSING", col)
        elif not pd.Series([value]).str.match(ID_PATTERNS["employee_id"]).iloc[0]:
            chk.add("ID_FORMAT", col, value)
        elif value not in self.accepted_employees:
            chk.add("REF_EMPLOYEE", col, value)
        return value

    def country_field(self, chk, row, col):
        value, outcome = st.map_category(row[col], self.countries)
        if outcome == st.CORRECTED:
            chk.add("COUNTRY_VARIANT", col, row[col], value)
        elif outcome == st.INVALID:
            value, ws = st.clean_text(row[col])       # keep unrecognised names, but tidy
            if ws == st.CORRECTED:
                chk.add("WHITESPACE", col, row[col], value)
                # A tidied name may now match the standard list
                again, outcome2 = st.map_category(value, self.countries)
                if outcome2 in (st.OK, st.CORRECTED):
                    return again
            chk.add("COUNTRY_UNKNOWN", col, row[col])
        elif outcome == st.MISSING:
            chk.add("OPTIONAL_MISSING", col)
        return value

    def new_check(self, dataset, row, key_col):
        office, _ = self.files.get(row["file_id"], (None, None))
        key = None if st.is_blank(row.get(key_col)) else str(row[key_col]).strip()
        return RecordCheck(dataset, int(row["file_id"]), int(row["source_row"]), office, key)

    # -- assemble results ---------------------------------------------------------
    @staticmethod
    def finish(dataset, checks_and_clean, raw_rows) -> DatasetResult:
        loadable, held, issues, cats = [], [], [], {}
        for (chk, clean), raw in zip(checks_and_clean, raw_rows):
            issues.extend(chk.issues)
            cat = chk.category
            cats[cat] = cats.get(cat, 0) + 1
            if cat in ("valid", "corrected"):
                loadable.append({**clean, "source_file_id": chk.file_id})
            else:
                held.append({"file_id": chk.file_id, "dataset": dataset, "record_key": chk.record_key,
                             "category": cat, "reasons": chk.reasons,
                             "raw_record": {k: (None if st.is_blank(v) else v)
                                            for k, v in raw.items() if k != "file_id"}})
        return DatasetResult(dataset, pd.DataFrame(loadable), held, issues, cats)

    @staticmethod
    def mark_duplicates(checks_and_clean, key_fn, rule="DUPLICATE_RECORD"):
        """Keep the FIRST loadable occurrence of each key; reject later ones."""
        seen = set()
        for chk, clean in checks_and_clean:
            if chk.category in ("manual_review", "rejected"):
                continue
            key = key_fn(clean)
            if key in seen:
                chk.add(rule, None, str(key))
            else:
                seen.add(key)

    # =============================================================================
    # Dataset validators
    # =============================================================================
    def validate_departments(self) -> DatasetResult:
        df = self.staged("departments")
        out = []
        for row in df.to_dict("records"):
            chk = self.new_check("departments", row, "department_id")
            clean = {c: self.text_field(chk, row, c, required=True)
                     for c in ["department_id", "department_name", "department_category", "directorate"]}
            out.append((chk, clean))
        result = self.finish("departments", out, df.to_dict("records"))
        self.departments = {r["department_id"]: r["department_id"] for _, r in result.loadable.iterrows()}
        self.department_names = {r["department_name"].casefold(): r["department_id"] for _, r in result.loadable.iterrows()}
        self._add_existing_reference()
        return result

    def validate_offices(self) -> DatasetResult:
        df = self.staged("offices")
        out = []
        for row in df.to_dict("records"):
            chk = self.new_check("offices", row, "office_id")
            clean = {c: self.text_field(chk, row, c, required=True)
                     for c in ["office_id", "office_name", "country", "region", "city", "office_type"]}
            out.append((chk, clean))
        result = self.finish("offices", out, df.to_dict("records"))
        self.offices.update({r["office_id"]: r["country"] for _, r in result.loadable.iterrows()})
        self._add_existing_reference()
        return result

    def _add_existing_reference(self):
        """On later runs the HQ files are not re-sent: use what is already in the warehouse."""
        with self.engine.connect() as conn:
            for r in conn.execute(text("SELECT department_id, department_name FROM dw.dim_department")):
                self.departments.setdefault(r.department_id, r.department_id)
                self.department_names.setdefault(r.department_name.casefold(), r.department_id)
            for r in conn.execute(text("SELECT office_id, country FROM dw.dim_location")):
                self.offices.setdefault(r.office_id, r.country)
            self.accepted_employees |= set(conn.execute(text("SELECT employee_id FROM dw.dim_employee")).scalars())

    def validate_employee_master(self) -> DatasetResult:
        df = self.staged("employee_master")
        rows = df.to_dict("records")
        out = []
        for row in rows:
            chk = self.new_check("employee_master", row, "employee_id")
            c = {}
            eid = None if st.is_blank(row["employee_id"]) else str(row["employee_id"]).strip()
            if eid is None:
                chk.add("PRIMARY_KEY_MISSING", "employee_id")
            elif not pd.Series([eid]).str.match(ID_PATTERNS["employee_id"]).iloc[0]:
                chk.add("ID_FORMAT", "employee_id", eid)
            c["employee_id"] = eid
            c["first_name"] = self.text_field(chk, row, "first_name", required=True)
            c["last_name"] = self.text_field(chk, row, "last_name", required=True)
            c["gender"] = self.category_field(chk, row, "gender", "gender")
            c["nationality"] = self.country_field(chk, row, "nationality")
            c["job_title"] = self.text_field(chk, row, "job_title")
            c["grade_level"] = self.grade_field(chk, row, "grade_level")
            c["department_id"] = self.department_field(chk, row, "department_id")
            c["source_office"] = self.office_field(chk, row, "source_office")
            c["employment_status"] = self.category_field(chk, row, "employment_status", "employment_status")
            c["supervisor_id"] = None if st.is_blank(row["supervisor_id"]) else str(row["supervisor_id"]).strip()

            c["hire_date"] = self.date_field(chk, row, "hire_date", required=True)
            c["date_of_birth"] = self.date_field(chk, row, "date_of_birth")
            c["contract_start_date"] = self.date_field(chk, row, "contract_start_date")
            c["contract_end_date"] = self.date_field(chk, row, "contract_end_date")
            c["last_updated"] = self.date_field(chk, row, "last_updated")

            # Country of assignment must agree with the office
            country = self.country_field(chk, row, "country_of_assignment")
            if country and c["source_office"] and country != self.offices[c["source_office"]]:
                chk.add("COUNTRY_MISMATCH", "country_of_assignment", country, self.offices[c["source_office"]])

            # Employment type: standardise, or infer if missing (documented rule)
            if st.is_blank(row["employment_type"]):
                c["employment_type"] = self.infer_employment_type(c)
                chk.add("EMPLOYMENT_TYPE_INFERRED", "employment_type", None, c["employment_type"])
            else:
                c["employment_type"] = self.category_field(chk, row, "employment_type", "employment_type")

            # Date consistency
            if c["contract_start_date"] and c["contract_end_date"] and c["contract_end_date"] < c["contract_start_date"]:
                chk.add("DATE_SEQUENCE", "contract_start_date|contract_end_date",
                        f"{c['contract_start_date']}|{c['contract_end_date']}")
            if c["date_of_birth"] and c["hire_date"] and c["date_of_birth"] >= c["hire_date"]:
                chk.add("DOB_AFTER_HIRE", "date_of_birth", c["date_of_birth"])
                c["date_of_birth"] = None
            out.append((chk, c))

        # Duplicates: keep the most recently updated version of each employee
        order = sorted(range(len(out)), key=lambda i: (out[i][1]["employee_id"] or "",
                                                        -(out[i][1]["last_updated"] or pd.Timestamp.min.date()).toordinal()))
        seen = {}
        for i in order:
            chk, c = out[i]
            if chk.category in ("manual_review", "rejected") or not c["employee_id"]:
                continue
            if c["employee_id"] in seen:
                identical = {k: v for k, v in c.items()} == seen[c["employee_id"]]
                chk.add("DUPLICATE_RECORD" if identical else "STALE_DUPLICATE", None, c["employee_id"])
            else:
                seen[c["employee_id"]] = {k: v for k, v in c.items()}

        self.accepted_employees |= {c["employee_id"] for chk, c in out if chk.category in ("valid", "corrected")}

        # Supervisors must themselves be accepted employees
        for chk, c in out:
            if c["supervisor_id"] and (c["supervisor_id"] not in self.accepted_employees
                                       or c["supervisor_id"] == c["employee_id"]):
                chk.add("REF_SUPERVISOR", "supervisor_id", c["supervisor_id"])
                c["supervisor_id"] = None
        return self.finish("employee_master", out, rows)

    @staticmethod
    def infer_employment_type(c: dict) -> str:
        title = (c.get("job_title") or "").lower()
        if title.endswith("intern"):
            return "Intern"
        if title.endswith("consultant"):
            return "Consultant"
        if not c.get("contract_end_date"):
            return "Continuing"
        start, end = c.get("contract_start_date"), c.get("contract_end_date")
        if start and (end - start).days >= 700:
            return "Fixed-term"
        return "Temporary"

    def validate_movements(self) -> DatasetResult:
        df = self.staged("movements")
        rows = df.to_dict("records")
        out = []
        for row in rows:
            chk = self.new_check("movements", row, "movement_id")
            c = {"movement_id": chk.record_key}
            if not c["movement_id"]:
                chk.add("PRIMARY_KEY_MISSING", "movement_id")
            c["employee_id"] = self.employee_ref(chk, row)
            c["movement_type"] = self.category_field(chk, row, "movement_type", "movement_type")
            c["effective_date"] = self.date_field(chk, row, "effective_date", required=True)
            c["previous_department"] = self.department_field(chk, row, "previous_department")
            c["new_department"] = self.department_field(chk, row, "new_department")
            c["previous_location"] = self.office_field(chk, row, "previous_location")
            c["new_location"] = self.office_field(chk, row, "new_location")
            c["previous_grade"] = self.grade_field(chk, row, "previous_grade", required=False)
            c["new_grade"] = self.grade_field(chk, row, "new_grade", required=False)
            out.append((chk, c))
        # The same movement can arrive twice: exact duplicates, or a transfer
        # reported by both the sending and the receiving office
        self.mark_duplicates(out, lambda c: c["movement_id"])
        return self.finish("movements", out, rows)

    def validate_workforce_snapshot(self) -> DatasetResult:
        df = self.staged("workforce_snapshot")
        rows = df.to_dict("records")
        out = []
        for row in rows:
            chk = self.new_check("workforce_snapshot", row, "snapshot_id")
            c = {"snapshot_id": chk.record_key}
            if not c["snapshot_id"]:
                chk.add("PRIMARY_KEY_MISSING", "snapshot_id")
            c["employee_id"] = self.employee_ref(chk, row)
            month, outcome = st.parse_month(row["reporting_month"], self.cfg["month_formats"])
            file_month = self.files.get(row["file_id"], (None, None))[1]
            if outcome in (st.INVALID, st.MISSING):
                chk.add("REQUIRED_MISSING", "reporting_month", row["reporting_month"])
                month = None
            elif file_month and month != file_month:
                chk.add("MONTH_MISMATCH", "reporting_month", month, file_month)
            c["reporting_month"] = month
            c["employment_status"] = self.category_field(chk, row, "employment_status", "employment_status")
            c["department_id"] = self.department_field(chk, row, "department_id")
            c["office_id"] = self.office_field(chk, row, "office_id")
            c["grade_level"] = self.grade_field(chk, row, "grade_level")
            out.append((chk, c))
        self.mark_duplicates(out, lambda c: (c["employee_id"], c["reporting_month"]))
        return self.finish("workforce_snapshot", out, rows)

    def validate_recruitment(self) -> DatasetResult:
        df = self.staged("recruitment")
        rows = df.to_dict("records")
        counts = ["application_count", "shortlisted_count", "interviewed_count", "offers_made", "candidates_hired"]
        max_apps = self.cfg["recruitment"]["max_plausible_applications"]
        out = []
        for row in rows:
            chk = self.new_check("recruitment", row, "recruitment_id")
            c = {"recruitment_id": chk.record_key}
            if not c["recruitment_id"]:
                chk.add("PRIMARY_KEY_MISSING", "recruitment_id")
            c["vacancy_id"] = self.text_field(chk, row, "vacancy_id", required=True)
            c["department_id"] = self.department_field(chk, row, "department_id")
            c["office_id"] = self.office_field(chk, row, "office_id")
            c["job_title"] = self.text_field(chk, row, "job_title")
            c["recruitment_status"] = self.category_field(chk, row, "recruitment_status", "recruitment_status")
            c["vacancy_open_date"] = self.date_field(chk, row, "vacancy_open_date")
            c["vacancy_close_date"] = self.date_field(chk, row, "vacancy_close_date")
            if st.is_blank(row["vacancy_open_date"]):
                chk.add("OPTIONAL_MISSING", "vacancy_open_date")
            if st.is_blank(row["vacancy_close_date"]) and c["recruitment_status"] != "Open":
                chk.add("OPTIONAL_MISSING", "vacancy_close_date")
            if c["vacancy_open_date"] and c["vacancy_close_date"] and c["vacancy_close_date"] < c["vacancy_open_date"]:
                chk.add("DATE_SEQUENCE", "vacancy_open_date|vacancy_close_date",
                        f"{c['vacancy_open_date']}|{c['vacancy_close_date']}")

            for col in counts:
                value, outcome = st.parse_count(row[col])
                if outcome != st.OK:
                    chk.add("NUMBER_INVALID", col, row[col])
                    value = None
                c[col] = value
            values = [c[col] for col in counts]
            if all(v is not None for v in values):
                if any(a < b for a, b in zip(values, values[1:])):
                    chk.add("FUNNEL_INCONSISTENT", "|".join(counts), "|".join(map(str, values)))
                if values[0] > max_apps:
                    chk.add("OUTLIER_APPLICATIONS", "application_count", values[0])
            out.append((chk, c))
        self.mark_duplicates(out, lambda c: c["recruitment_id"])
        return self.finish("recruitment", out, rows)

    def run(self) -> dict:
        results = {}
        for name in ["departments", "offices", "employee_master", "movements", "workforce_snapshot", "recruitment"]:
            results[name] = getattr(self, f"validate_{name}")()
        self.attach_exit_dates(results)
        return results

    def attach_exit_dates(self, results):
        """dim_employee needs an exit date for every Separated employee. It comes from
        the accepted exit movement; if none was accepted, use last_updated (flagged)."""
        mv = results["movements"].loadable
        exits = {}
        if not mv.empty:
            ex = mv[mv["movement_type"].isin(EXIT_TYPES)]
            exits = ex.groupby("employee_id")["effective_date"].max().to_dict()
        master = results["employee_master"]
        if master.loadable.empty:
            return
        def exit_date(r):
            if r["employment_status"] != "Separated":
                return None
            return exits.get(r["employee_id"]) or r["last_updated"] or r["hire_date"]
        master.loadable["exit_date"] = master.loadable.apply(exit_date, axis=1)
        estimated = master.loadable[(master.loadable["employment_status"] == "Separated")
                                    & ~master.loadable["employee_id"].isin(exits)]
        for _, r in estimated.iterrows():
            chk = RecordCheck("employee_master", r["source_file_id"], None, r["source_office"], r["employee_id"])
            chk.add("EXIT_DATE_ESTIMATED", "exit_date", None, r["exit_date"])
            master.issues.extend(chk.issues)
