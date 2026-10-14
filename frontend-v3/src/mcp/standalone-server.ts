/**
 * standalone-server.ts — MCP Streamable HTTP host without Vite.
 *
 * Hosts the vendored MCP stack (vite-middleware.ts transport +
 * mcp-server.ts tool registry + server-tools.ts data tools) on a plain
 * node:http server for the Docker sidecar. The Vite dev surface at /__mcp is
 * unchanged — this is the same registry on a different host.
 *
 * Differences from the dev-server mount:
 *  - Bearer-token auth on every request (MCP_TOKEN, fail-closed).
 *  - Host allowlist (MCP_ALLOWED_HOSTS, comma-separated) — the MCP
 *    spec's DNS-rebinding guard, applied in-process even though nginx
 *    already enforces the host allowlist at the edge.
 *  - Browser tools (overlays, HITL, devtools) stay registered but fail
 *    closed: there is no HMR channel here, so callClient is a stub that
 *    rejects — the designed "no connected client" behavior.
 *  - Any path is treated as the MCP endpoint; the container is
 *    single-purpose. `/healthz` is the one exception (compose
 *    healthcheck).
 */

import http from "node:http";
import crypto from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import { createMcpMiddleware } from "./vite-middleware";
import { createMcpServer } from "./mcp-server";
import { createMcpHandlers } from "./mcp-handlers";
import type { CallClient } from "./channel/server";
import type { ClientFunctions } from "./client-functions";

const PORT = parseInt(process.env.MCP_PORT || "8100", 10);
const MCP_TOKEN = process.env.MCP_TOKEN;
if (!MCP_TOKEN) {
  console.error("[MCP] MCP_TOKEN must be set — refusing to start.");
  process.exit(1);
}
const EXPECTED_AUTH = Buffer.from(`Bearer ${MCP_TOKEN}`);

const ALLOWED_HOSTS = new Set(
  (process.env.MCP_ALLOWED_HOSTS || "localhost,127.0.0.1")
    .split(",")
    .map((h) => h.trim())
    .filter(Boolean),
);

// No Vite HMR channel exists in the sidecar — browser-bound tools
// (proposeQuery, renderToolOverlay, inspector) keep the designed
// no-client behavior and fail fast.
const callClient: CallClient<ClientFunctions> = (() =>
  Promise.reject(
    new Error("no connected client — browser tools are dev-only"),
  )) as CallClient<ClientFunctions>;

const mcpHandlers = createMcpHandlers(callClient);
const mcp = createMcpMiddleware(() => createMcpServer(mcpHandlers));

function authorized(req: IncomingMessage): boolean {
  const header = Buffer.from(String(req.headers.authorization || ""));
  return (
    header.length === EXPECTED_AUTH.length &&
    crypto.timingSafeEqual(header, EXPECTED_AUTH)
  );
}

const server = http.createServer(
  (req: IncomingMessage, res: ServerResponse) => {
    const url = new URL(req.url || "/", "http://localhost");

    if (url.pathname === "/healthz") {
      res.writeHead(200, { "Content-Type": "text/plain" });
      res.end("ok");
      return;
    }

    // Host allowlist (DNS-rebinding guard): nginx forwards the original
    // Host, already filtered per surface — re-checking here keeps the
    // sidecar safe even if reached directly on the compose network.
    const host = String(req.headers.host || "").split(":")[0];
    if (!ALLOWED_HOSTS.has(host)) {
      res.writeHead(403, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "host not allowed" }));
      return;
    }

    if (!authorized(req)) {
      res.writeHead(401, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "unauthorized" }));
      return;
    }

    // Single-purpose container: every remaining path is the MCP
    // endpoint (nginx maps /mcp/ here). The vendored middleware routes
    // on /__mcp internally.
    req.url = "/__mcp";
    void mcp(req, res, () => {
      res.writeHead(404, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "not found" }));
    });
  },
);

server.listen(PORT, "0.0.0.0", () => {
  console.log(`[MCP] sidecar listening on :${PORT} (Streamable HTTP)`);
});
