"""Per-country subdivision labels for the frontend picker.

The SubdivisionSelector in the SPA labels its dropdown generically
("Subdivision"), but countries call their first-level divisions different
things (Ireland → County, Canada → Province and Territory). This map
localizes that label so the picker reads naturally per country.

Terms follow each country's common English name for its principal
first-level administrative division. Synthetic non-sovereign codes
(Wales/Scotland/... from ``non_sovereign_territories``) are included too.

Unknown / unlisted countries fall back to "Subdivision". Extend the map
as needed — keyed by ISO 3166-1 alpha-2, matching ``CountryPipelineProfile.iso2``.
"""

SUBDIVISION_LABELS = {
    # ── User-specified (2026-10-06) ────────────────────────────────────
    "IE": "County",
    "CA": "Province and Territory",

    # ── Europe ─────────────────────────────────────────────────────────
    "GB": "County",
    "FR": "Department",
    "DE": "State",
    "AT": "State",
    "CH": "Canton",
    "IT": "Region",
    "ES": "Autonomous Community",
    "PT": "District",
    "NL": "Province",
    "BE": "Province",
    "LU": "Canton",
    "DK": "Region",
    "SE": "County",
    "NO": "County",
    "FI": "Region",
    "PL": "Voivodeship",
    "CZ": "Region",
    "SK": "Region",
    "HU": "County",
    "RO": "County",
    "BG": "Province",
    "GR": "Region",
    "HR": "County",
    "SI": "Region",
    "RS": "District",
    "AL": "County",
    "MK": "Region",
    "BA": "Entity",
    "ME": "Municipality",
    "LT": "County",
    "LV": "Municipality",
    "EE": "County",
    "IS": "Region",
    "CY": "District",
    "MT": "Region",
    "MD": "District",
    "UA": "Oblast",
    "BY": "Region",
    "TR": "Province",
    "RU": "Federal Subject",
    "KZ": "Region",
    "GE": "Region",
    "AM": "Province",
    "AZ": "District",

    # ── Americas ───────────────────────────────────────────────────────
    "US": "State",
    "MX": "State",
    "BR": "State",
    "AR": "Province",
    "CL": "Region",
    "PE": "Department",
    "CO": "Department",
    "VE": "State",
    "BO": "Department",
    "PY": "Department",
    "UY": "Department",
    "EC": "Province",
    "GY": "Region",
    "SR": "District",
    "PA": "Province",
    "CR": "Province",
    "NI": "Department",
    "HN": "Department",
    "SV": "Department",
    "GT": "Department",
    "BZ": "District",
    "CU": "Province",
    "DO": "Province",
    "HT": "Department",
    "JM": "Parish",
    "TT": "Region",
    "BS": "District",

    # ── Africa ─────────────────────────────────────────────────────────
    "ZA": "Province",
    "NG": "State",
    "EG": "Governorate",
    "MA": "Region",
    "DZ": "Province",
    "TN": "Governorate",
    "LY": "District",
    "SD": "State",
    "ET": "Region",
    "KE": "County",
    "TZ": "Region",
    "UG": "District",
    "GH": "Region",
    "CI": "District",
    "SN": "Region",
    "ML": "Region",
    "NE": "Region",
    "BF": "Province",
    "BJ": "Department",
    "TG": "Region",
    "CM": "Region",
    "GA": "Province",
    "CG": "Department",
    "CD": "Province",
    "AO": "Province",
    "ZM": "Province",
    "ZW": "Province",
    "MZ": "Province",
    "MW": "District",
    "NA": "Region",
    "BW": "District",
    "LS": "District",
    "SZ": "Region",
    "MG": "Region",
    "MU": "District",
    "SC": "District",
    "MR": "Region",
    "GM": "Division",
    "GN": "Region",
    "SL": "District",
    "LR": "County",
    "SO": "Region",
    "DJ": "Region",
    "ER": "Region",
    "CF": "Prefecture",
    "TD": "Province",
    "GQ": "Province",
    "RW": "Province",
    "BI": "Province",
    "KM": "Island",

    # ── Asia ───────────────────────────────────────────────────────────
    "IN": "State",
    "PK": "Province",
    "BD": "Division",
    "LK": "Province",
    "NP": "Province",
    "BT": "District",
    "MM": "Region",
    "TH": "Province",
    "LA": "Province",
    "KH": "Province",
    "VN": "Province",
    "CN": "Province",
    "JP": "Prefecture",
    "KR": "Province",
    "KP": "Province",
    "TW": "County",
    "MN": "Province",
    "MY": "State",
    "ID": "Province",
    "PH": "Province",
    "TL": "Municipality",
    "UZ": "Region",
    "KG": "Region",
    "TJ": "Region",
    "TM": "Region",
    "AF": "Province",
    "IR": "Province",
    "IQ": "Governorate",
    "SY": "Governorate",
    "LB": "Governorate",
    "JO": "Governorate",
    "SA": "Province",
    "YE": "Governorate",
    "OM": "Governorate",
    "AE": "Emirate",
    "QA": "Municipality",
    "BH": "Governorate",
    "KW": "Governorate",
    "IL": "District",
    "PS": "Governorate",

    # ── Oceania ────────────────────────────────────────────────────────
    "AU": "State and Territory",
    "NZ": "Region",
    "PG": "Province",
    "FJ": "Division",
    "SB": "Province",
    "VU": "Province",
    "WS": "District",
    "TO": "Division",

    # ── Caribbean ──────────────────────────────────────────────────────
    "GD": "Parish",
    "BB": "Parish",
    "LC": "Quarter",
    "VC": "Parish",
    "AG": "Parish",
    "DM": "Parish",
    "KN": "Parish",

    # ── Synthetic non-sovereign codes (non_sovereign_territories) ──────
    "WL": "Principal Area",   # Wales
    "XS": "Council Area",     # Scotland
    "EN": "County",           # England
    "IM": "Parish",           # Isle of Man
    "GG": "Parish",           # Guernsey
    "JE": "Parish",           # Jersey
}

DEFAULT_SUBDIVISION_LABEL = "Subdivision"


def get_subdivision_label(country_code):
    """Return the localized subdivision label for an ISO-2 code.

    Falls back to the generic "Subdivision" for unknown / empty codes.
    """
    if not country_code:
        return DEFAULT_SUBDIVISION_LABEL
    return SUBDIVISION_LABELS.get(
        str(country_code).strip().upper(), DEFAULT_SUBDIVISION_LABEL
    )
