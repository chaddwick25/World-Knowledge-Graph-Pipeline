import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { randomUUID } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";

/**
 * Creates a Connect middleware that exposes an MCP server via Streamable HTTP.
 *
 * - POST `/__mcp` — JSON-RPC messages (init, tool calls, etc.)
 * - GET  `/__mcp` — SSE stream for server→client notifications
 * - DELETE `/__mcp` — terminate the session
 *
 * @param createServer — factory called for each new MCP session
 *                        (one McpServer instance per session)
 */
export function createMcpMiddleware(createServer: () => McpServer) {
  // Map sessionId → transport (one transport per MCP session)
  const transports: Record<string, StreamableHTTPServerTransport> = {};

  /**
   * Connect middleware compatible with `server.middlewares.use()`.
   * Routes requests to `/__mcp`.
   */
  function middleware(
    req: IncomingMessage & { body?: unknown },
    res: ServerResponse,
    next: () => void,
  ) {
    const url = new URL(req.url || "/", "http://localhost");
    if (url.pathname !== "/__mcp") {
      return next();
    }

    const method = req.method?.toUpperCase();

    switch (method) {
      case "POST": {
        handlePost(req, res);

        break;
      }
      case "GET": {
        handleGet(req, res);

        break;
      }
      case "DELETE": {
        handleDelete(req, res);

        break;
      }
      default: {
        res.writeHead(405);
        res.end("Method Not Allowed");
      }
    }
  }

  // ── POST: messages JSON-RPC ─────────────────────────────
  async function handlePost(
    req: IncomingMessage & { body?: unknown },
    res: ServerResponse,
  ) {
    try {
      const body = await readJsonBody(req);
      const sessionId = req.headers["mcp-session-id"] as string | undefined;

      let transport: StreamableHTTPServerTransport;

      if (sessionId && transports[sessionId]) {
        // Existing session
        transport = transports[sessionId];
      } else if (!sessionId && isInitializeRequest(body)) {
        // New session
        transport = new StreamableHTTPServerTransport({
          sessionIdGenerator: () => randomUUID(),
          onsessioninitialized: (sid) => {
            console.log(`[MCP] Session initialized: ${sid}`);
            transports[sid] = transport;
          },
        });

        transport.onclose = () => {
          const sid = transport.sessionId;
          if (sid && transports[sid]) {
            console.log(`[MCP] Session closed: ${sid}`);
            delete transports[sid];
          }
        };

        const mcpServer = createServer();
        await mcpServer.connect(transport);
        await transport.handleRequest(req, res, body);
        return;
      } else if (sessionId && !transports[sessionId]) {
        // Expired session (e.g. after server restart)
        // 404 → the MCP client must re-initialize (MCP spec)
        res.writeHead(404, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            jsonrpc: "2.0",
            error: {
              code: -32_000,
              message: "Session not found. Please reinitialize.",
            },
            id: null,
          }),
        );
        return;
      } else {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            jsonrpc: "2.0",
            error: {
              code: -32_000,
              message: "Bad Request: No valid session ID provided",
            },
            id: null,
          }),
        );
        return;
      }

      await transport.handleRequest(req, res, body);
    } catch (error) {
      console.error("[MCP] Error handling POST:", error);
      if (!res.headersSent) {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            jsonrpc: "2.0",
            error: { code: -32_603, message: "Internal server error" },
            id: null,
          }),
        );
      }
    }
  }

  // ── GET: SSE stream ─────────────────────────────────────
  async function handleGet(req: IncomingMessage, res: ServerResponse) {
    const sessionId = req.headers["mcp-session-id"] as string | undefined;
    if (sessionId && !transports[sessionId]) {
      res.writeHead(404);
      res.end("Session not found");
      return;
    }
    if (!sessionId) {
      res.writeHead(400);
      res.end("Missing session ID");
      return;
    }

    const transport = transports[sessionId]!;
    await transport.handleRequest(req, res);
  }

  // ── DELETE: termination ─────────────────────────────────
  async function handleDelete(req: IncomingMessage, res: ServerResponse) {
    const sessionId = req.headers["mcp-session-id"] as string | undefined;
    if (sessionId && !transports[sessionId]) {
      res.writeHead(404);
      res.end("Session not found");
      return;
    }
    if (!sessionId) {
      res.writeHead(400);
      res.end("Missing session ID");
      return;
    }

    const transport = transports[sessionId]!;
    await transport.handleRequest(req, res);
  }

  return middleware;
}

// ──────────────── Helpers ──────────────────────────────────

/** Reads the JSON body of an HTTP request. */
function readJsonBody(
  req: IncomingMessage & { body?: unknown },
): Promise<unknown> {
  if (req.body !== undefined) {
    return Promise.resolve(req.body);
  }

  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      try {
        const text = Buffer.concat(chunks).toString("utf8");
        resolve(JSON.parse(text));
      } catch (error_) {
        reject(error_);
      }
    });
    req.on("error", reject);
  });
}
