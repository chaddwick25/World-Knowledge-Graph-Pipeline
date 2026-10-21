"""
queryable_countries - the queryable-country allowlist (2026-10-21).

A TEMPORARY demo gate: QUERYABLE_COUNTRY_ALLOWLIST (comma-separated ISO
codes, e.g. "IE,JM,KR") restricts which countries the query surfaces
serve. Empty/unset = no gate (every processed country is queryable).
The owner removes the gate after the fresh-DB e2e reinitializes the
planetary file.
"""
from django.conf import settings


def allowlist_codes():
    """The allowlisted ISO codes (uppercased set), or None = no gate."""
    raw = getattr(settings, "QUERYABLE_COUNTRY_ALLOWLIST", "") or ""
    codes = {c.strip().upper() for c in raw.split(",") if c.strip()}
    return codes or None


def is_country_queryable(country_code):
    """True when the country may be queried. No gate = always True;
    a gate with no code = nothing is queryable."""
    codes = allowlist_codes()
    if codes is None:
        return True
    if not country_code:
        return False
    return str(country_code).strip().upper() in codes
