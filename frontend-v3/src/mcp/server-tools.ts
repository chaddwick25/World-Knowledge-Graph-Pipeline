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

import crypto from "node:crypto";

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

  /**
   * Factor-table coverage for a (country, snapshot) — the G4 data-
   * availability check. Lets the agent verify which latent spaces
   * (spectral, drift, embeddings) are populated BEFORE proposing a
   * query that needs them.
   */
  async getFactorAvailability(params: {
    countryCode: string;
    snapshotDate?: string;
  }): Promise<any> {
    const q = new URLSearchParams({ country_code: params.countryCode });
    if (params.snapshotDate) q.set("snapshot_date", params.snapshotDate);
    const res = await fetch(`${backendBase()}/nca/factor-availability/?${q.toString()}`);
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        detail = data?.error || data?.detail || JSON.stringify(data);
      } catch {
        // non-JSON error body — keep the status code
      }
      throw new Error(`Django factor-availability failed: ${detail}`);
    }
    return res.json();
  },

  /**
   * Start a research-orchestrator run (doppelganger report and friends)
   * as an async job. `research/stream` is a synchronous SSE endpoint —
   * there is no submit/poll API — so the job shape lives here: this
   * call returns a job_id immediately while the SSE stream is consumed
   * in the background into an in-memory buffer. getDoppelgangerStatus /
   * getDoppelgangerResult read that buffer.
   *
   * Dedupe: an identical brief already running returns its job_id —
   * the run burns GPU on the research LLM (2070), so re-submits share.
   */
  async runDoppelganger(params: {
    brief: string;
    countryCode?: string;
    snapshotDate?: string;
    subdivisionQid?: string;
  }): Promise<any> {
    pruneResearchJobs();
    for (const [id, job] of researchJobs) {
      if (job.status === "running" && job.brief === params.brief &&
          job.countryCode === (params.countryCode ?? null)) {
        return { job_id: id, status: "running", deduped: true };
      }
    }

    const q = new URLSearchParams({ prompt: params.brief });
    if (params.countryCode) q.set("country_code", params.countryCode);
    if (params.snapshotDate) q.set("snapshot_date", params.snapshotDate);
    if (params.subdivisionQid) q.set("subdivision_qid", params.subdivisionQid);

    const jobId = crypto.randomUUID();
    researchJobs.set(jobId, {
      status: "running",
      events: [],
      brief: params.brief,
      countryCode: params.countryCode ?? null,
      startedAt: Date.now(),
    });
    // Fire-and-forget: consume the SSE stream into the job record.
    void consumeResearchStream(
      jobId, `${backendBase()}/nca/research/stream/?${q.toString()}`,
    );
    return { job_id: jobId, status: "running" };
  },

  /** Poll a running job — latest progress events + status. */
  async getDoppelgangerStatus(params: { jobId: string }): Promise<any> {
    const job = researchJobs.get(params.jobId);
    if (!job) throw new Error(`unknown job_id ${params.jobId}`);
    return {
      job_id: params.jobId,
      status: job.status,
      event_count: job.events.length,
      recent_events: job.events.slice(-10),
      error: job.error ?? null,
    };
  },

  /** Fetch the final result once status is "done". */
  async getDoppelgangerResult(params: { jobId: string }): Promise<any> {
    const job = researchJobs.get(params.jobId);
    if (!job) throw new Error(`unknown job_id ${params.jobId}`);
    if (job.status === "error") {
      throw new Error(`job ${params.jobId} failed: ${job.error}`);
    }
    if (job.status !== "done") {
      return { job_id: params.jobId, status: job.status };
    }
    return { job_id: params.jobId, status: "done", result: job.result };
  },
};

// ── Research job bookkeeping (process-local) ────────────────────────
//
// research/stream is synchronous SSE, so the async-job surface MCP
// clients get is emulated here: a job record accumulates SSE events
// until the terminal "done"/"error" event. The map lives in module
// state — the Vite dev server and the Docker sidecar share this code,
// so both hosts give the same behavior. Jobs expire after an hour;
// the map is ephemeral by design (sidecar restart = jobs gone).

interface ResearchJob {
  status: "running" | "done" | "error";
  events: Array<Record<string, unknown>>;
  result?: unknown;
  error?: string;
  brief: string;
  countryCode: string | null;
  startedAt: number;
}

const researchJobs = new Map<string, ResearchJob>();
const RESEARCH_JOB_TTL_MS = 60 * 60 * 1000;

function pruneResearchJobs() {
  const cutoff = Date.now() - RESEARCH_JOB_TTL_MS;
  for (const [id, job] of researchJobs) {
    if (job.startedAt < cutoff) researchJobs.delete(id);
  }
}

/** Consume a research/stream SSE response into the job record. */
async function consumeResearchStream(jobId: string, url: string) {
  const job = researchJobs.get(jobId);
  if (!job) return;
  try {
    const res = await fetch(url);
    if (!res.ok || !res.body) {
      throw new Error(`Django research/stream failed: HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let event = "message";
        let data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        let payload: Record<string, unknown> = {};
        try {
          payload = JSON.parse(data);
        } catch {
          payload = { raw: data };
        }
        job.events.push({ event, ...payload });
        if (event === "done") {
          job.status = "done";
          job.result = payload.result;
        } else if (event === "error") {
          job.status = "error";
          job.error = String(payload.error ?? "unknown error");
        }
      }
    }
    if (job.status === "running") {
      job.status = "error";
      job.error = "stream closed before the done event";
    }
  } catch (error_) {
    job.status = "error";
    job.error = error_ instanceof Error ? error_.message : String(error_);
  }
}

export type ServerTools = typeof serverTools;
