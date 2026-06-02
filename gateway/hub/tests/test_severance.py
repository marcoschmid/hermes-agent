"""Tests for gateway.hub.severance — Phase-6b Step-7 write-severance flag.

The flag gates ALL hub->MC writes so the shared MC api_key (id=60) can be
revoked. Default OFF (writes enabled) keeps current behaviour; ON makes every
sink a no-op. Read at call-time so the cutover flip needs no client rebuild.
"""
import pytest

from gateway.hub.severance import ENV_WRITE_SEVERED, mc_writes_severed


def test_default_off_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(ENV_WRITE_SEVERED, raising=False)
    assert mc_writes_severed() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes", "on"])
def test_truthy_values_enable_severance(monkeypatch, value) -> None:
    monkeypatch.setenv(ENV_WRITE_SEVERED, value)
    assert mc_writes_severed() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_falsy_values_keep_writes_enabled(monkeypatch, value) -> None:
    monkeypatch.setenv(ENV_WRITE_SEVERED, value)
    assert mc_writes_severed() is False
