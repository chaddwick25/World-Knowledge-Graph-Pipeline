import type { CallClient } from "./channel/server";
import type { ClientFunctions } from "./client-functions";

export function createMcpHandlers(callClient: CallClient<ClientFunctions>) {
  return {
    // ── Vue DevTools (built-in) ──
    async getInspectorTree() {
      return await callClient("getInspectorTree");
    },
    async getComponentState(nodeId: string) {
      return await callClient("getComponentState", nodeId);
    },

    // ── MapQA HITL (domain-specific) ──
    async proposeQuery(params: {
      template: string;
      concepts: Array<{ type: string; text: string | null; confidence?: number }>;
      roles?: Array<{ concept_type: string; role: string }>;
      confidence: number;
      country_code?: string;
    }) {
      return await callClient("proposeQuery", params);
    },
    async getApprovalState() {
      return await callClient("getApprovalState");
    },
    async getQueryProposal() {
      return await callClient("getQueryProposal");
    },
    async renderToolOverlay(overlay: Record<string, unknown>) {
      return await callClient("renderToolOverlay", overlay);
    },
  };
}

export type McpHandlers = ReturnType<typeof createMcpHandlers>;
