import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { McpHandlers } from "./mcp-handlers";
import { serverTools } from "./server-tools";

export function createMcpServer(mcpHandlers: McpHandlers): McpServer {
  const server = new McpServer({
    name: "Vue MCP Server",
    version: "1.0.0",
  });

  // ──────────────── Vue DevTools Tools (built-in) ────────────────

  server.registerTool(
    "getInspectorTree",
    {
      description: "Get the Vue component tree in markdown tree syntax format.",
    },
    async () => {
      const tree = await mcpHandlers.getInspectorTree();
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(tree),
          },
        ],
      };
    },
  );

  server.registerTool(
    "getComponentState",
    {
      description: "Get the state of a Vue component.",
      inputSchema: {
        nodeId: z
          .string()
          .describe("The node ID from the inspector tree (e.g. 'app-1:20')"),
      },
    },
    async ({ nodeId }) => {
      const state = await mcpHandlers.getComponentState(nodeId);
      return {
        content: [{ type: "text" as const, text: JSON.stringify(state) }],
      };
    },
  );

  // ──────────────── MapQA HITL Tools (domain-specific) ────────────────

  server.registerTool(
    "proposeQuery",
    {
      description:
        "Push a parsed MapQA query to the frontend for user confirmation. " +
        "The user sees a modal with the template, concepts, and roles, and can " +
        "approve, edit, or reject the proposed query.",
      inputSchema: {
        template: z.string().describe("The classified template name"),
        concepts: z
          .array(
            z.object({
              type: z.string(),
              text: z.string().nullable(),
              confidence: z.number().optional(),
            }),
          )
          .describe("Extracted concept slots"),
        roles: z
          .array(
            z.object({
              concept_type: z.string(),
              role: z.string(),
            }),
          )
          .optional()
          .describe("Assigned functional roles"),
        confidence: z.number().describe("Parser confidence (0-1)"),
        country_code: z.string().optional().describe("ISO country code"),
      },
    },
    async (params) => {
      const result = await mcpHandlers.proposeQuery(params);
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result) }],
      };
    },
  );

  server.registerTool(
    "getApprovalState",
    {
      description:
        "Check whether the user has approved, edited, or rejected the " +
        "proposed query. Returns the current approval state and any user edits.",
    },
    async () => {
      const result = await mcpHandlers.getApprovalState();
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result) }],
      };
    },
  );

  server.registerTool(
    "getQueryProposal",
    {
      description:
        "Get the full query proposal state including the proposed query, " +
        "approval state, user edits, and execution result (if any).",
    },
    async () => {
      const result = await mcpHandlers.getQueryProposal();
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result) }],
      };
    },
  );

  // ──────────────── WorldKG Query Tools (Node-side — no browser needed) ────────────────

  server.registerTool(
    "structuredSearch",
    {
      description:
        "Structured OSM tag search against the WorldKG pipeline. Finds entities " +
        "matching OSM tags (e.g. {\"amenity\": \"cafe\"}) using triple-space scoring " +
        "(name + geo + class). Returns {country_code, count, results: [{osm_type, " +
        "osm_id, tags, wkg_class, geom: {lat, lon}, scores}]}.",
      inputSchema: {
        countryCode: z
          .string()
          .describe("Country name or ISO code (e.g. 'Belize', 'US')"),
        queryTags: z
          .record(z.string(), z.string())
          .describe("OSM tags to match, e.g. {\"amenity\": \"cafe\"}"),
        lat: z.number().optional().describe("Optional geographic anchor latitude"),
        lon: z.number().optional().describe("Optional geographic anchor longitude"),
        rdfType: z
          .string()
          .optional()
          .describe("Optional WorldKG class filter (e.g. 'wkgs:Cafe')"),
        topK: z
          .number()
          .int()
          .min(1)
          .max(100)
          .optional()
          .describe("Max results (default 20)"),
        snapshotDate: z
          .string()
          .optional()
          .describe("Snapshot date string, e.g. '2025_12_31'"),
        subdivisionQid: z
          .string()
          .optional()
          .describe("Wikidata QID to scope the search to a subdivision"),
      },
    },
    async (params) => {
      const result = await serverTools.structuredSearch(params);
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result) }],
      };
    },
  );

  server.registerTool(
    "nameSearch",
    {
      description:
        "Natural-language name search (any language/script) against the WorldKG " +
        "pipeline. The romanizer handles cross-script matching transparently " +
        "(e.g. English 'paris bagueete' matches Korean '파리바게뜨'). Returns the " +
        "same result shape as structuredSearch.",
      inputSchema: {
        countryCode: z
          .string()
          .describe("Country name or ISO code (e.g. 'Belize', 'US')"),
        naturalQuery: z
          .string()
          .describe("Name to search for in any language or script"),
        lat: z.number().optional().describe("Optional geographic anchor latitude"),
        lon: z.number().optional().describe("Optional geographic anchor longitude"),
        rdfType: z
          .string()
          .optional()
          .describe("Optional WorldKG class filter (e.g. 'wkgs:Cafe')"),
        topK: z
          .number()
          .int()
          .min(1)
          .max(100)
          .optional()
          .describe("Max results (default 20)"),
        snapshotDate: z
          .string()
          .optional()
          .describe("Snapshot date string, e.g. '2025_12_31'"),
        subdivisionQid: z
          .string()
          .optional()
          .describe("Wikidata QID to scope the search to a subdivision"),
      },
    },
    async (params) => {
      const result = await serverTools.nameSearch(params);
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result) }],
      };
    },
  );

  server.registerTool(
    "templateQuery",
    {
      description:
        "Kuhn's-template geospatial question against the WorldKG pipeline. Runs " +
        "the MapQA parser (classifies into one of ~13 templates) then the executor. " +
        "Returns {query, parsed: {template, concepts, confidence}, result: {answer, " +
        "trace, results: [{osm_type, osm_id, tags, wkg_class, lat, lon, distance_m}], " +
        "error?}}. Use for questions like 'Which bars are within 50m of Hollywood Blvd?'",
      inputSchema: {
        query: z.string().describe("The geospatial question in natural language"),
        countryCode: z
          .string()
          .optional()
          .describe("Country name or ISO code (optional — parser may infer)"),
        snapshotDate: z
          .string()
          .optional()
          .describe("Snapshot date string, e.g. '2025_12_31'"),
      },
    },
    async (params) => {
      const result = await serverTools.templateQuery(params);
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result) }],
      };
    },
  );

  server.registerTool(
    "getFactorAvailability",
    {
      description:
        "Check factor-table coverage (G4) for a country and snapshot — which latent " +
        "spaces are populated (spectral, drift, amenity embeddings, entity " +
        "embeddings). Call this BEFORE proposing a query that needs a specific " +
        "space (e.g. spectral analysis needs the 'spectral' flag true). Returns " +
        "{country_code, snapshot_date, spectral, drift, amenity_embeddings, " +
        "entity_embeddings}.",
      inputSchema: {
        countryCode: z
          .string()
          .describe("ISO country code or name (e.g. 'BZ', 'Belize')"),
        snapshotDate: z
          .string()
          .optional()
          .describe("Snapshot date string, e.g. '2025_12_31' (defaults to latest)"),
      },
    },
    async (params) => {
      const result = await serverTools.getFactorAvailability(params);
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result) }],
      };
    },
  );

  // ──────────────── Overlay Tool (browser-side — renders on the map) ────────────────

  server.registerTool(
    "renderToolOverlay",
    {
      description:
        "Render a tool's output on the WorldKG map as a dedicated overlay, so the " +
        "user can SEE what each step produced. Accepts either a raw data-tool result " +
        "(pass `result`) or extracted entities (pass `entities`). Kinds: 'markers' " +
        "(default — circle markers), 'scaled-markers' (radius by score/diffusion), " +
        "'radius' (markers + circle around anchor), 'markers-line' (markers + line " +
        "to anchor). Returns {status, id, overlayCount}.",
      inputSchema: {
        tool: z
          .enum(["structuredSearch", "nameSearch", "templateQuery", "other"])
          .describe("Which tool produced this overlay (drives the color)"),
        kind: z
          .enum(["markers", "scaled-markers", "radius", "markers-line"])
          .optional()
          .describe("Overlay kind (default 'markers')"),
        result: z
          .any()
          .optional()
          .describe("Raw data-tool result — entities are extracted from it"),
        entities: z
          .array(
            z.object({
              osmType: z.string().optional(),
              osmId: z.union([z.string(), z.number()]).optional(),
              lat: z.number(),
              lon: z.number(),
              name: z.string().optional(),
              wkgClass: z.string().optional(),
              score: z.number().optional(),
              distanceM: z.number().optional(),
            }),
          )
          .optional()
          .describe("Entities to draw (alternative to `result`)"),
        anchor: z
          .object({ lat: z.number(), lon: z.number() })
          .optional()
          .describe("Anchor coordinate (required for 'radius' and 'markers-line')"),
        radius: z
          .number()
          .optional()
          .describe("Radius in meters (for 'radius' kind, e.g. 50 = 50m)"),
        color: z.string().optional().describe("Optional marker color override"),
      },
    },
    async (params) => {
      const result = await mcpHandlers.renderToolOverlay(params);
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result) }],
      };
    },
  );

  return server;
}
