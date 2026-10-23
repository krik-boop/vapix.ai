import logging

from app.core.logging import SecretRedactingFilter, setup_logging
from tests.conftest import FAKE_AMO_TOKEN, FAKE_HH_TOKEN


def test_known_secret_values_are_redacted(settings, capsys):
    setup_logging(settings)
    log = logging.getLogger("test")
    log.warning("token=%s domain=%s", FAKE_AMO_TOKEN, "test-account.amocrm.ru")
    log.warning("hh %s", FAKE_HH_TOKEN)
    err = capsys.readouterr().err
    assert FAKE_AMO_TOKEN not in err
    assert FAKE_HH_TOKEN not in err
    assert "test-account.amocrm.ru" not in err
    assert "***" in err


def test_patterns_are_redacted_even_for_unknown_values():
    f = SecretRedactingFilter([])
    assert "abc123" not in f.redact("Authorization: Bearer abc123")
    assert "zzz" not in f.redact("POST /token refresh_token=zzz&grant_type=refresh_token")
    assert "pa55" not in f.redact("postgresql+psycopg://user:pa55@db:5432/x")


def test_exception_text_is_redacted(settings, capsys):
    setup_logging(settings)
    try:
        raise RuntimeError(f"failed with {FAKE_AMO_TOKEN}")
    except RuntimeError:
        logging.getLogger("test").exception("boom")
    err = capsys.readouterr().err
    assert FAKE_AMO_TOKEN not in err
    assert "RuntimeError" in err
