"""Tests for configuration parsing."""

import pytest

from src.config import _parse_telegram_user_ids


def test_parse_telegram_user_ids_from_csv(monkeypatch):
    monkeypatch.setenv("TELEGRAM_USER_IDS", "123, 456,123")

    assert _parse_telegram_user_ids() == [123, 456]


def test_parse_telegram_user_ids_required(monkeypatch):
    monkeypatch.delenv("TELEGRAM_USER_IDS", raising=False)

    with pytest.raises(ValueError, match="TELEGRAM_USER_IDS is required"):
        _parse_telegram_user_ids()

def test_parse_telegram_user_ids_invalid_csv(monkeypatch):
    monkeypatch.setenv("TELEGRAM_USER_IDS", "123,abc")

    with pytest.raises(ValueError, match="TELEGRAM_USER_IDS"):
        _parse_telegram_user_ids()
