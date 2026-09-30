import { devtools } from "@vue/devtools-kit";

type Devtools = typeof devtools;

function stripCircular<T>(obj: T): T {
  const seen = new WeakSet();
  return JSON.parse(
    JSON.stringify(obj, (_key, value) => {
      if (typeof value === "object" && value !== null) {
        if (seen.has(value)) return "[Circular]";
        seen.add(value);
      }
      return value;
    }),
  );
}

// ── MapQA HITL types ──────────────────────────────────────────

interface QueryProposal {
  template: string;
  concepts: Array<{ type: string; text: string | null; confidence?: number }>;
  roles?: Array<{ concept_type: string; role: string }>;
  confidence: number;
  country_code?: string;
}

interface ApprovalState {
  approvalState: "idle" | "pending" | "approved" | "edited" | "rejected";
  proposedQuery: QueryProposal | null;
  userEdits: Array<{ type: string; text: string }> | null;
  executionResult: any | null;
}

// ── Pinia store accessor (lazy — Pinia must be initialized first) ──

let _store: any = null;

async function getQueryProposalStore(): Promise<any> {
  if (_store) return _store;
  // Dynamic import to avoid circular dependency at module load time
  const { useQueryProposalStore } = await import("../stores/queryProposalStore");
  // Pinia instance is attached to the app by createPinia() in main.js
  // We access it via the global Pinia instance
  _store = useQueryProposalStore();
  return _store;
}

export function createClientFunctions(devtools: Devtools) {
  return {
    // ── Vue DevTools (built-in) ──
    async getInspectorTree() {
      const tree = await devtools.api.getInspectorTree({
        inspectorId: "components",
        filter: "",
      });
      return tree;
    },
    async getComponentState(nodeId: string) {
      const state = await devtools.api.getInspectorState({
        inspectorId: "components",
        nodeId,
      });
      const strippedState = stripCircular(state);
      const componentState = strippedState.state?.filter(
        (s: any) => s.type === "setup",
      );
      return componentState;
    },

    // ── MapQA HITL (domain-specific) ──
    async proposeQuery(params: QueryProposal): Promise<{ status: string }> {
      const store = await getQueryProposalStore();
      store.proposeQuery(params);
      return { status: "pending" };
    },
    async getApprovalState(): Promise<ApprovalState> {
      const store = await getQueryProposalStore();
      return {
        approvalState: store.approvalState,
        proposedQuery: store.proposedQuery,
        userEdits: store.userEdits,
        executionResult: store.executionResult,
      };
    },
    async getQueryProposal(): Promise<ApprovalState> {
      const store = await getQueryProposalStore();
      return {
        approvalState: store.approvalState,
        proposedQuery: store.proposedQuery,
        userEdits: store.userEdits,
        executionResult: store.executionResult,
      };
    },

    // ── Agent overlay (domain-specific) ──
    async renderToolOverlay(overlay: Record<string, unknown>) {
      const { useOverlayStore } = await import("../stores/overlayStore");
      const store = useOverlayStore();
      return store.renderOverlay(overlay);
    },
  };
}

export type ClientFunctions = ReturnType<typeof createClientFunctions>;
