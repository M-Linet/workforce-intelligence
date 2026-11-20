"""
Read CSV and Excel files safely, and standardise their column names.

Reading rules:
  - every value is read as TEXT (dtype=str): nothing is converted or guessed yet
  - only truly empty cells become missing; text such as 'N/A' is KEPT as text,
    so the validator can see and report exactly what the office typed
"""

import re
from pathlib import Path

import pandas as pd


class UnreadableFileError(Exception):
    """The file exists but its content cannot be parsed as a table."""


def read_table(path: Path) -> pd.DataFrame:
    options = dict(dtype=str, keep_default_na=False, na_values=[""])
    try:
        if path.suffix.lower() == ".csv":
            return pd.read_csv(path, encoding="utf-8", **options)
        return pd.read_excel(path, engine="openpyxl", **options)
    except Exception as exc:                      # decoding, zip, parser errors...
        raise UnreadableFileError(f"{type(exc).__name__}: {exc}") from exc


def normalise_name(column) -> str:
    """'  Employee Id ' -> 'employee_id';  'Eff-Dt' -> 'eff_dt'."""
    name = str(column).strip().lower()
    name = re.sub(r"[\s\-]+", "_", name)
    return re.sub(r"[^0-9a-z_]", "", name)


def standardise_columns(df: pd.DataFrame, dataset: str, pipeline_cfg: dict):
    """Rename columns to the standard template.

    Returns (df_with_standard_columns, unknown_columns, missing_columns, recognised_share).
    Unknown columns are dropped (and reported); missing ones are added as empty.
    """
    aliases = {**pipeline_cfg["column_aliases"]["common"],
               **pipeline_cfg["column_aliases"].get(dataset, {})}
    expected = pipeline_cfg["expected_columns"][dataset]

    mapping, unknown = {}, []
    for col in df.columns:
        norm = normalise_name(col)
        std = norm if norm in expected else aliases.get(norm)
        if std in expected and std not in mapping.values():
            mapping[col] = std
        else:
            unknown.append(str(col))

    out = df[list(mapping)].rename(columns=mapping)
    missing = [c for c in expected if c not in out.columns]
    for c in missing:
        out[c] = pd.NA
    recognised_share = len(mapping) / len(expected)
    return out[expected], unknown, missing, recognised_share
