"""Minimal logging setup (ADJ-SPK-026 / SF4). Nothing in this app configured logging
anywhere -- Python's logging module falls back to a "handler of last resort" that only
surfaces WARNING and above when no handler is configured, so `speakeasy.engine.remote`'s
INFO-level fallback log (R5.1: "log, don't interrupt") was silently dropped in every real
run. Sets up a stderr handler for the "speakeasy" logger namespace ONLY -- never touches
the root logger or reconfigures any library's own logging (sherpa-onnx, http.server, etc.
keep whatever they already do).
"""

import logging
import os

_configured = False


def configure_logging() -> None:
    """Idempotent: safe to call from both speakeasy/__main__.py and
    speakeasy/service.py's launcher without double-adding a handler."""
    global _configured
    if _configured:
        return
    level_name = os.environ.get("SPEAKEASY_LOG", "info").upper()
    level = getattr(logging, level_name, logging.INFO)

    logger = logging.getLogger("speakeasy")
    logger.setLevel(level)
    logger.propagate = False  # stop here -- never reaches (or reconfigures) the root logger

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
    logger.addHandler(handler)

    _configured = True
