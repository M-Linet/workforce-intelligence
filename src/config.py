"""
Central configuration and path management.

All paths are built relative to the project root (found from this file's
location), so code works no matter which directory it is run from.
"""

from pathlib import Path

import yaml

# src/config.py -> parent is src/ -> parent of that is the project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
REJECTED_DIR = DATA_DIR / "rejected"
SYNTHETIC_DIR = DATA_DIR / "synthetic"
LOG_DIR = PROJECT_ROOT / "logs"


def load_yaml(filename: str) -> dict:
    """Load a YAML file from the config/ folder and return it as a dict."""
    path = CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Check the file name and that it has been saved."
        )
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    # Self-test: python -m src.config
    print("Project root:", PROJECT_ROOT)
    cfg = load_yaml("organisation.yaml")
    print("Organisation:", cfg["organisation"]["name"])
    print("Offices:", len(cfg["offices"]))
    print("CONFIG OK")