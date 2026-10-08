# Deterministic Cross-Script Name Romanization

> **Focus:** how the `romanizing_names` service converts non-Latin and
> diacritic-bearing OSM names to a Latin phonetic fingerprint for pg_trgm
> similarity search, without any neural model, API call, or network
> dependency.
>
> **Key idea:** romanization is not translation. It is a mechanical mapping
> of characters to their Latin sound equivalents using Unicode
> decomposition + lookup tables. The result doesn't need to be a "correct"
> romanization. It needs to be deterministic and consistent so that
> `similarity("paribagetteu", "paris bagueete")` scores high in
> PostgreSQL's trigram index.

---

## 1. Why This Exists

FastText `cc.en.300` cannot tokenize Hangul (Korean script). Korean tokens
are out-of-vocabulary and collapse into weakly meaningful subword
representations. A user searching for "paris bagueete" (English
misspelling) would never match the Korean OSM name `파리바게뜨` ("Paris
Baguette") through semantic embeddings alone.

The romanizer solves this by producing a **phonetic fingerprint**, a
deterministic Latin-string representation of any name regardless of script.
These fingerprints are persisted in `OsmEntity.name_romanized` and indexed
with PostgreSQL's `pg_trgm` GIN index for fast similarity search at query
time.

---

## 2. Architecture

**Romanizing service (in-process, no network):**

`RomanizerRegistry.auto_romanize(text)`:
1. Detect script:
   - Hangul blocks (U+AC00–U+D7AF)? → `HangulRomanizer`
   - Combining marks / diacritic chars? → `DiacriticRomanizer`
   - Otherwise → `IdentityRomanizer`
2. `romanizer.romanize(text)` → Latin string

- Latency: ~8 microseconds per call
- Dependencies: Python stdlib only (`unicodedata`, `re`)
- No model file, no training, no weights, no API

### 2.1 The Three Romanizers

| Romanizer | File | Lines | Detects | Example |
|---|---|---|---|---|
| Hangul | `hangul_romanizer.py` | 105 | Hangul Unicode blocks (U+AC00–U+D7AF) | `파리바게뜨` → `paribagetteu` |
| Diacritic | `diacritic_romanizer.py` | 120 | Combining marks (Mn category) + precomposed diacritic chars | `café` → `cafe`, `Señor` → `senor` |
| Identity | `identity_romanizer.py` | 31 | Fallback for plain Latin / other scripts | `Paris Baguette` → `paris baguette` |

### 2.2 The Registry

`registry.py` (77 lines) auto-detects the script and dispatches to the
appropriate romanizer. Detection is text-based, no language or country
configuration needed:

```python
class RomanizerRegistry:
    @staticmethod
    def auto_romanize(text: str) -> str:
        if _contains_hangul(text):
            return HangulRomanizer().romanize(text)
        if _has_diacritics(text):
            return DiacriticRomanizer().romanize(text)
        return IdentityRomanizer().romanize(text)
```

### 2.3 File Map

```
backend/semantic_search/services/romanizing_names/
    __init__.py              15 lines   Package doc
    base_romanizer.py        52 lines   AbstractRomanizer base class
    identity_romanizer.py    31 lines   Latin/English fallback
    hangul_romanizer.py     105 lines   Korean (NFKD + lookup tables)
    diacritic_romanizer.py  120 lines   French, Spanish, Irish, etc.
    registry.py              77 lines   RomanizerRegistry + auto_romanize()

Total: 400 lines, 64 KB of source code
```

---

## 3. How Each Romanizer Works

### 3.1 HangulRomanizer: Korean

Hangul syllables are composed blocks of three parts:
- **Choseong** (initial consonant): ㄱ ㄴ ㄷ ㄹ ㅁ ㅂ ㅅ ㅇ ㅈ ㅊ ㅋ ㅌ ㅍ ㅎ
- **Jungseong** (vowel): ㅏ ㅐ ㅑ ㅓ ㅔ ㅕ ㅗ ㅛ ㅜ ㅠ ㅡ ㅣ
- **Jongseong** (final consonant, optional): ㄱ ㄴ ㄷ ㄹ ㅁ ㅂ ㅅ ㅇ ㅈ ㅊ ㅋ ㅌ ㅍ ㅎ

The romanizer decomposes each syllable block using Unicode NFKD
normalization, then maps each component to its Latin equivalent via lookup
tables:

```
파리바게뜨
  - 파  → ㅂ(p) + ㅏ(a)              → "pa"
  - 리  → ㄹ(r) + ㅣ(i)              → "ri"
  - 바  → ㅂ(p) + ㅏ(a)              → "pa"
  - 게  → ㄱ(g) + ㅔ(e)              → "ge"
  - 뜨  → ㄸ(tt) + ㅡ(eu)            → "tteu"
  → "paribagetteu"
```

The choseong/jungseong/jongseong mappings cover all modern Hangul
components. The jongseong (final consonant) table was added after initial
testing showed `서울` producing `seou` instead of `seoul`.

### 3.2 DiacriticRomanizer: French, Spanish, Irish, etc.

Uses Unicode NFKD normalization to decompose precomposed characters into
their base + combining mark form, then strips all combining marks (category
Mn):

```
café
  - NFKD: c a f é → c a f e + ◌́ (combining acute)
  - Strip Mn: c a f e
  → "cafe"

Señor Plaza
  - NFKD: S e ñ o r   P l a z a → S e n + ◌̃  o r   P l a z a
  - Strip Mn: S e n o r   P l a z a
  → "senor plaza"

Banc na hÉireann
  - NFKD: É → E + ◌́
  - Strip Mn: E
  → "banc na heireann"
```

This covers all Latin-script languages that use diacritical marks: French
(é, è, ê, ë, ç), Spanish (ñ, á, í, ó, ú), Irish (á, é, í, ó, ú), German (ä,
ö, ü, ß), Vietnamese (ă, â, đ, ê, ô, ơ, ư), Portuguese, Italian,
Scandinavian (å, æ, ø), and more.

### 3.3 IdentityRomanizer: Fallback

Lowercases the text, collapses whitespace, and strips non-alphanumeric
punctuation. The fallback for plain Latin text and any script without a
dedicated romanizer:

```
Paris Baguette → "paris baguette"
O'Brien's      → "o'brien s"
```

---

## 4. Database Integration

### 4.1 The name_romanized Column

Migration `0017_osmentity_name_romanized` adds:

```sql
ALTER TABLE semantic_search_osmentity
  ADD COLUMN name_romanized TEXT;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX idx_osmentity_name_romanized_trgm
  ON semantic_search_osmentity
  USING GIN (name_romanized gin_trgm_ops);
```

The column is nullable. Entities that haven't been processed yet have
`NULL`. The search view gracefully handles `NULL` values.

### 4.2 The romanize_names Command

```
backend/semantic_search/management/commands/romanize_names.py
```

Processes entities where `name_romanized IS NULL`. Idempotent and
resumable, re-running skips already-processed entities.

```
docker compose exec backend python manage.py romanize_names --country-code KR
```

Performance (observed on KR, 1.7M named entities):
- Start: ~2,500 entities/sec
- Steady state: ~1,500 entities/sec (degrades as the index grows)
- Total: 1,705,121 entities in 1,133 seconds (~19 minutes)

### 4.3 Storage Cost

| Component | Per-entity | KR (1M named) | Full planet (50M named) |
|---|---|---|---|
| `name_romanized` column | ~40 bytes | ~23 MB | ~8 GB |
| pg_trgm GIN index | ~120 bytes | ~70 MB | ~8 GB |
| **Total DB overhead** | ~160 bytes | **~93 MB** | **~16 GB** |

The source code itself is 64 KB, smaller than a single photo.

### 4.4 Reset semantics

The jamo mappings (`_CHOSEONG`, `_JUNGSEONG`, `_JONGSEONG`, `_COMPAT`,
merged in `_ALL_MAP`) are module constants in `hangul_romanizer.py`,
git-tracked source. They are not stored in any database. A DB reset
(`scripts/drop_databases_v2.sh`) wipes the `name_romanized` column with
the rest of the vectors DB but never touches the mapping. `romanize_names`
rebuilds the column deterministically from the `name` tags, so a reset
costs a command run, not data. The registry's `auto_romanize()` detects
the script per name; one command run covers every script present.

---

## 5. Search Integration

### 5.1 The Two Parallel Matching Paths

When a user searches for a name, the search view runs two matching paths in
parallel and combines their scores:

**FASTTEXT PATH (semantic):**
- Query → 300D embedding
- Entity → 300D embedding
- Cosine distance (`<=>` operator)
- `S_name = 0.42` (weak, OOV collapse on Hangul)

**ROMANIZER PATH (deterministic):**
- Query → "senor plaza" (`DiacriticRomanizer`)
- Entity `name_romanized` = "senyoreu peulraja" (`HangulRomanizer`,
  precomputed at ingest)
- n-gram Jaccard `similarity()`
- `S_xscript = 0.38` (weak, phonetic overlap but Korean adds syllables
  -reu, peul-ra-ja)

Combine: `S_total = 1.80` (combined signal).

The FastText path alone would struggle. `cc.en.300` can't tokenize Hangul,
so all Korean text collapses to similar OOV vectors. The romanizer path
compensates with a deterministic phonetic signal that doesn't depend on a
neural model.

### 5.2 Request Flow

1. **Frontend (browser)**: `POST /api/nca/semantic-triplet-search/`
   `{ "country_code": "South Korea", "natural_query": "paris bagueete",
   "top_k": 20 }`
2. **Parse request**: `country_code = "South Korea"`,
   `natural_query = "paris bagueete"`.
3. **Resolve country → bbox**: "South Korea" → `OsmBoundary` → bbox =
   (124.6, 33.2, 131.9, 38.6).
4. **Build initial queryset**: `SELECT * FROM semantic_search_osmentity
   WHERE geom WITHIN polygon AND gv_tags_embedding IS NOT NULL AND tags ?|
   array['amenity','shop','tourism',...] AND country_code = 'KR'`.
5. **Romanize the query** (once, ~8μs):
   `RomanizerRegistry.romanize_query("paris bagueete")` → no Hangul, no
   diacritics → `IdentityRomanizer` → "paris bagueete".
6. **Generate FastText embedding** (for semantic matching) → 300D vector.
7. **SQL candidate retrieval** (Postgres, ~1ms with indexes):
   `SELECT id, tags, gv_tags_embedding, name_romanized,
   gv_tags_embedding <=> query_vec AS cosine_distance ... ORDER BY
   cosine_distance LIMIT 500`. Indexes used: GiST on `geom` (bbox filter),
   GIN on `tags` (`has_any_keys`), exact cosine scan on
   `gv_tags_embedding` (`+ 0`; HNSW deliberately not used).
8. **Score each candidate** (Python, in-memory). For entity "파리바게뜨"
   (Korean bakery), `name_romanized: "paribagetteu"`:
   - `S_name = 1 - cosine_distance` (FastText, weak on Hangul)
   - `S_xscript = cross_script_name_score("paris bagueete", "파리바게뜨")`:
     romanize entity → "paribagetteu"; n-gram
     Jaccard("paris bagueete", "paribagetteu") → 0.38
   - `S_geo = 0.0` (no lat/lon given)
   - `S_class = 1.0` (if `wkg_class` matches)
   - `S_total = S_name + S_xscript + S_geo + S_class`
9. **Sort + return top K**: sort by `S_total DESC`, take top 20, serialize
   to JSON. `200 OK` with results.

---

## 6. What This Is NOT

| Task | What it does | Needs AI? | This service? |
|---|---|---|---|
| Translate "Hello" → "Bonjour" | Understand meaning + pick equivalent | Yes (neural) | No |
| Romanize 파리바게뜨 → paribagetteu | Map each character to its sound | No (lookup table) | **Yes** |
| Romanize café → cafe | Strip the accent mark | No (Unicode normalize) | **Yes** |
| Translate 파리바게뜨 → "Paris Baguette" | Know the brand name in English | Yes (neural) | No |

The romanizer produces a **phonetic fingerprint**, not a translation. The
name doesn't need to be a "correct" romanization. It needs to be
deterministic and consistent so that trigram similarity works.

---

## 7. Historical Context

| Year | Technology | What it did |
|---|---|---|
| 1960s | ASCII transliteration | Converting non-Latin text for teletype machines |
| 1980s | Unicode normalization | Standardized decomposition of accented characters |
| 1995 | ISO 9 | Cyrillic→Latin transliteration standard |
| 2000 | Revised Romanization of Korean | Official Hangul→Latin standard (South Korea) |
| 2007 | PostgreSQL pg_trgm | Indexed n-gram similarity in the database |

The romanizer combines 1980s Unicode science with 2007 database indexing.
The only "modern" part is the pg_trgm GIN index. Everything else is
classical computational linguistics.

---

## 8. Scope and Future Extensions

### 8.1 Currently Supported

| Romanizer | Languages | Script |
|---|---|---|
| Hangul | Korean | Hangul (U+AC00–U+D7AF) |
| Diacritic | French, Spanish, Irish, German, Vietnamese, Portuguese, Italian, Scandinavian | Latin with diacritical marks |
| Identity | English, plain Latin, fallback | Latin (no diacritics) |

### 8.2 Deliberately Excluded

Chinese Hanzi and Japanese Kanji are **intentionally excluded** from this
framework. Pinyin/readings do not reliably preserve English phonetic
similarity for meaning-based names. A Chinese name like "巴黎" (Paris)
romanizes to "bālí" via pinyin, but the phonetic overlap with "paris" is
weak. For ideographic scripts, a future `name:en` fallback or multilingual
embeddings would be more appropriate.

### 8.3 Future Phonetic Romanizers

| Romanizer | Languages | Estimated size | Estimated lines |
|---|---|---|---|
| Cyrillic | Russian, Ukrainian, Bulgarian, Serbian | 4 KB | ~50 |
| Greek | Greek | 4 KB | ~40 |
| Georgian | Georgian | 4 KB | ~50 |
| Kana | Japanese (hiragana + katakana) | 8 KB | ~100 |
| Hebrew | Hebrew | 4 KB | ~40 |
| Armenian | Armenian | 4 KB | ~50 |
| Devanagari | Hindi, Marathi, Nepali | 8 KB | ~100 |
| Thai | Thai | 12 KB | ~120 |
| Arabic | Arabic | 8 KB | ~100 |

All 11 phonetic scripts together: ~120 KB, ~1,100 lines of source code.
Smaller than a single FastText model file by a factor of 5,000.

---

## 9. Decoupling from Ingestion

The romanizer is deliberately decoupled from the country pipeline:

- **Not part of** `VectorStorageService`, `EmbeddingService`,
  `CountryPipelineProfile`, or any staging/COPY/INSERT SQL.
- Runs as a **standalone management command** on any schedule.
- **Idempotent and resumable**, re-running skips processed entities.
- Can be **rerun after a database reset**. `migrate` recreates the schema,
  `romanize_names` regenerates the values.
- **New romanizers can be added** by creating a module + registry entry,
  with no changes to pipeline code or the query path.

After a `drop_databases.sh` + rebuild, the only commands needed to restore
romanization:

```bash
docker compose exec backend python manage.py migrate --database=vectors
docker compose exec backend python manage.py romanize_names
```

The code is the source of truth; the database is just a cache.

---

## 10. File References

| Component | Path |
|---|---|
| Romanizer package | `backend/semantic_search/services/romanizing_names/` |
| Base class | `backend/semantic_search/services/romanizing_names/base_romanizer.py` |
| Hangul romanizer | `backend/semantic_search/services/romanizing_names/hangul_romanizer.py` |
| Diacritic romanizer | `backend/semantic_search/services/romanizing_names/diacritic_romanizer.py` |
| Identity romanizer | `backend/semantic_search/services/romanizing_names/identity_romanizer.py` |
| Registry | `backend/semantic_search/services/romanizing_names/registry.py` |
| Management command | `backend/semantic_search/management/commands/romanize_names.py` |
| Migration | `backend/worldkg_nca/migrations/0017_osmentity_name_romanized.py` |
| Model field | `backend/worldkg_nca/models.py` (`OsmEntity.name_romanized`) |
| Search integration | `backend/worldkg_nca/views/search.py` (`_cross_script_name_score`) |
| Unit tests | `backend/tests/unit/test_romanizers.py` (45 tests) |
| Implementation plan | `docs/plans/NAME_ROMANIZING_FRAMEWORK_PLAN.md` |
