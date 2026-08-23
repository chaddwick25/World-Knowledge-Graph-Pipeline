"""Name romanizing framework for cross-script similarity search.

Deterministic, script-detect-based romanization of OSM entity names into
Latin characters for use with PostgreSQL pg_trgm similarity indexing.

Supported scripts:
  - Hangul (Korean) — phonetic, King Sejong 1443
  - Diacritic Latin (French, Spanish, Irish, Vietnamese, etc.) — NFKD strip
  - Identity (English, German without umlauts, etc.) — lowercase + strip

Adding a new script:
  1. Create a romanizer module in this package
  2. Register it in registry.py
  3. Re-run romanize_names command — auto-detect handles the rest
"""
