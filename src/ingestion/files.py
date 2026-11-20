"""
Discover submitted files and identify them.

For every file under data/raw/ we work out:
  - what it is (office, dataset, reporting month) from its name
  - its SHA-256 hash: a fingerprint of its CONTENT, so a file re-sent under a
    different name is still recognised as the same data
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from src.config import RAW_DIR

SUPPORTED_SUFFIXES = {".csv", ".xlsx"}


@dataclass
class SourceFile:
    path: Path
    rel_path: str               # e.g. 'OFF09/OFF09_movements_2025-03.xlsx'
    office_id: str | None
    dataset: str | None
    reporting_month: str | None
    size_bytes: int
    sha256: str


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    """Hash in 1 MB chunks so even very large files never need to fit in memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def identify(path: Path, pipeline_cfg: dict, raw_dir: Path = RAW_DIR) -> SourceFile:
    rel = path.relative_to(raw_dir).as_posix()
    office = dataset = month = None

    reference = {v: k for k, v in pipeline_cfg["reference_files"].items()}
    if rel in reference:
        office, dataset = "HQ", reference[rel]
    else:
        m = re.match(pipeline_cfg["file_name_pattern"], path.name)
        if m:
            office, dataset, month = m["office_id"], m["dataset"], m["reporting_month"]

    return SourceFile(path, rel, office, dataset, month, path.stat().st_size, sha256_of(path))


def discover(pipeline_cfg: dict, raw_dir: Path = RAW_DIR) -> list[SourceFile]:
    """All supported files under raw_dir, reference files first, then in name order
    (so offices' months are always processed in time order)."""
    files = [p for p in raw_dir.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES]
    found = [identify(p, pipeline_cfg, raw_dir) for p in files]
    return sorted(found, key=lambda f: (f.office_id != "HQ", f.rel_path))
