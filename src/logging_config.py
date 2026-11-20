"""
Logging set-up shared by the whole pipeline.

Every message goes to two places:
  - the console (INFO and above), so you can watch a run
  - logs/pipeline.log (DEBUG and above), a permanent record for troubleshooting
The database audit trail (audit.etl_audit_log) is written separately by the
pipeline; this file log also captures problems that happen before the
database can be reached.
"""

import logging

from src.config import LOG_DIR

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:               # already configured: don't add duplicate handlers
        return logger
    logger.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)-7s | %(message)s"))

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_DIR / "pipeline.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FORMAT))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger
