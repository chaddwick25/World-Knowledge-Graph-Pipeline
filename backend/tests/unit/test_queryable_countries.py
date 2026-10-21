"""The queryable-country allowlist (2026-10-21): the temporary demo
gate. QUERYABLE_COUNTRY_ALLOWLIST restricts which countries the query
surfaces serve; empty = no gate. Removed after the fresh-DB e2e."""
import pytest

from django.test import override_settings

from core.services.queryable_countries import (
    allowlist_codes, is_country_queryable,
)


class TestAllowlistCodes:
    def test_no_gate_when_unset(self):
        with override_settings(QUERYABLE_COUNTRY_ALLOWLIST=""):
            assert allowlist_codes() is None

    def test_parses_comma_separated_iso(self):
        with override_settings(QUERYABLE_COUNTRY_ALLOWLIST="IE, JM,KR"):
            assert allowlist_codes() == {"IE", "JM", "KR"}

    def test_uppercases_and_dedupes(self):
        with override_settings(QUERYABLE_COUNTRY_ALLOWLIST="ie,jm,IE"):
            assert allowlist_codes() == {"IE", "JM"}


class TestIsCountryQueryable:
    def test_no_gate_allows_everything(self):
        with override_settings(QUERYABLE_COUNTRY_ALLOWLIST=""):
            assert is_country_queryable("MX") is True
            assert is_country_queryable(None) is True

    def test_gate_admits_allowlisted(self):
        with override_settings(QUERYABLE_COUNTRY_ALLOWLIST="IE,JM,KR"):
            assert is_country_queryable("IE") is True
            assert is_country_queryable("jm") is True  # case-insensitive
            assert is_country_queryable(" jm ") is True  # whitespace

    def test_gate_rejects_others(self):
        with override_settings(QUERYABLE_COUNTRY_ALLOWLIST="IE,JM,KR"):
            assert is_country_queryable("MX") is False
            assert is_country_queryable("BZ") is False

    def test_gate_rejects_missing_code(self):
        with override_settings(QUERYABLE_COUNTRY_ALLOWLIST="IE,JM,KR"):
            assert is_country_queryable(None) is False
            assert is_country_queryable("") is False
