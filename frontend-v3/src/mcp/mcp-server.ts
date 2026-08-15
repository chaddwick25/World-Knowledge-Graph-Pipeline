import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { McpHandlers } from "./mcp-handlers";

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

  return server;
}
