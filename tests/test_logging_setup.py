import logging

import pytest

import speakeasy.logging_setup as logging_setup


@pytest.fixture
def reset_speakeasy_logger(monkeypatch):
    """Isolates each test from the module-level idempotency flag and from any
    handler a previous test (or the real app, if this ran in-process) attached
    to the "speakeasy" logger."""
    monkeypatch.setattr(logging_setup, "_configured", False)
    logger = logging.getLogger("speakeasy")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers = []
    yield logger
    logger.handlers = original_handlers
    logger.level = original_level
    logger.propagate = original_propagate


def test_fallback_log_line_reaches_real_stderr(capsys, reset_speakeasy_logger, monkeypatch):
    """The exact bug SF4/ADJ-SPK-026 describes: speakeasy.engine.remote's R5.1
    fallback log was silently dropped because nothing configured logging anywhere.
    This exercises the real path -- configure, then log through the actual child
    logger remote.py uses, and check it landed on real stderr, not caplog's
    separate capture mechanism."""
    monkeypatch.delenv("SPEAKEASY_LOG", raising=False)
    logging_setup.configure_logging()

    remote_logger = logging.getLogger("speakeasy.engine.remote")
    remote_logger.info("remote transcribe on linux failed (refused); falling back to local")

    captured = capsys.readouterr()
    assert "remote transcribe on linux failed (refused); falling back to local" in captured.err

def test_configure_logging_is_idempotent(reset_speakeasy_logger, monkeypatch):
    """Counts only exact-type StreamHandler instances (not len(handlers)) --
    pytest's own logging instrumentation (LogCaptureHandler, a StreamHandler
    subclass) may independently attach handlers to any logger during a full
    suite run; that's pytest's business, not something configure_logging()
    controls or should be judged against."""
    monkeypatch.delenv("SPEAKEASY_LOG", raising=False)
    logging_setup.configure_logging()
    logging_setup.configure_logging()
    logging_setup.configure_logging()
    own_handlers = [h for h in logging.getLogger("speakeasy").handlers
                     if type(h) is logging.StreamHandler]
    assert len(own_handlers) == 1

def test_speakeasy_log_env_var_controls_level(capsys, reset_speakeasy_logger, monkeypatch):
    monkeypatch.setenv("SPEAKEASY_LOG", "warning")
    logging_setup.configure_logging()

    logger = logging.getLogger("speakeasy.engine.remote")
    logger.info("this info line should NOT appear")
    logger.warning("this warning line SHOULD appear")

    captured = capsys.readouterr()
    assert "this info line should NOT appear" not in captured.err
    assert "this warning line SHOULD appear" in captured.err

def test_default_level_is_info_when_env_var_unset(reset_speakeasy_logger, monkeypatch):
    monkeypatch.delenv("SPEAKEASY_LOG", raising=False)
    logging_setup.configure_logging()
    assert logging.getLogger("speakeasy").level == logging.INFO

def test_never_touches_the_root_logger(reset_speakeasy_logger, monkeypatch):
    monkeypatch.delenv("SPEAKEASY_LOG", raising=False)
    root = logging.getLogger()
    original_root_handlers = list(root.handlers)
    logging_setup.configure_logging()
    assert list(root.handlers) == original_root_handlers  # untouched
    assert logging.getLogger("speakeasy").propagate is False
