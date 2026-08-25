/**
 * server-tools.ts — Node-side MCP data tools.
 *
 * These tools run inside the Vite dev server process and call the Django
 * backend directly via fetch. They do NOT require a browser tab — the agent
 * can use them headless (`goose run -t`). This is the "data tools live in
 * Node" design from docs/plans/MCP_AGENT_MVP_PLAN.md §5.
 *
 * The human UI path (SemanticSearchPanel) and these tools are independent
 * HTTP clients of the same backend endpoints — no shared implementation.
 */

const DEFAULT_BACKEND_URL = "http://localhost:8000/api";

function backendBase(): string {
  return process.env.WORLDKG_BACKEND_URL || DEFAULT_BACKEND_URL;
}

async function postJson(path: string, body: Record<string, unknown>): Promise<any> {
  const res = await fetch(`${backendBase()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data?.error || data?.detail || JSON.stringify(data);
    } catch {
      // non-JSON error body — keep the status code
    }
    throw new Error(`Django ${path} failed: ${detail}`);
  }
  return res.json();
}

// ── Shared param mapping ────────────────────────────────────────────────

interface SearchParams {
  countryCode: string;
  lat?: number;
  lon?: number;
  rdfType?: string;
  topK?: number;
  snapshotDate?: string;
  subdivisionQid?: string;
}

function searchBody(params: SearchParams, extra: Record<string, unknown>) {
  const body: Record<string, unknown> = {
    country_code: params.countryCode,
    top_k: params.topK ?? 20,
    ...extra,
  };
  if (params.lat != null) body.lat = params.lat;
  if (params.lon != null) body.lon = params.lon;
  if (params.rdfType) body.rdf_type = params.rdfType;
  if (params.snapshotDate) body.snapshot_date = params.snapshotDate;
  if (params.subdivisionQid) body.subdivision_qid = params.subdivisionQid;
  return body;
}

// ── Data tools ──────────────────────────────────────────────────────────

export const serverTools = {
  /**
   * Structured (JSON) tag search — POST /api/nca/semantic-triplet-search/
   * with query_tags. Triple-space scoring: name + geo + class.
   */
  async structuredSearch(params: {
    countryCode: string;
    queryTags: Record<string, string>;
    lat?: number;
    lon?: number;
    rdfType?: string;
    topK?: number;
    snapshotDate?: string;
    subdivisionQid?: string;
  }): Promise<any> {
    const { queryTags, ...rest } = params;
    return postJson("/nca/semantic-triplet-search/", {
      ...searchBody(rest, {}),
      query_tags: queryTags,
    });
  },

  /**
   * Natural-language name search — POST /api/nca/semantic-triplet-search/
   * with natural_query. The romanizer handles cross-script matching
   * (Hangul↔Latin, diacritic stripping) transparently.
   */
  async nameSearch(params: {
    countryCode: string;
    naturalQuery: string;
    lat?: number;
    lon?: number;
    rdfType?: string;
    topK?: number;
    snapshotDate?: string;
    subdivisionQid?: string;
  }): Promise<any> {
    const { naturalQuery, ...rest } = params;
    return postJson("/nca/semantic-triplet-search/", {
      ...searchBody(rest, {}),
      natural_query: naturalQuery,
    });
  },

  /**
   * Kuhn's-template query — POST /api/nca/execute-query/. Runs the MapQA
   * parser (template classification) + executor against the factor tables
   * and PostGIS. Returns { query, parsed, result: { answer, trace, results } }.
   */
  async templateQuery(params: {
    query: string;
    countryCode?: string;
    snapshotDate?: string;
  }): Promise<any> {
    const body: Record<string, unknown> = { query: params.query };
    if (params.countryCode) body.country_code = params.countryCode;
    if (params.snapshotDate) body.snapshot_date = params.snapshotDate;
    return postJson("/nca/execute-query/", body);
  },
};

export type ServerTools = typeof serverTools;
