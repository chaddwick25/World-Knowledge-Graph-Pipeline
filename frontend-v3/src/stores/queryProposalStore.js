/**
 * queryProposalStore — Pinia store for MapQA HITL (Human-in-the-Loop).
 *
 * Holds the agent-proposed query (template + concepts + roles) and the
 * user's approval/edit state. Used by:
 *   - QueryConfirmationModal.vue (renders the proposal for user review)
 *   - MCP client-functions.ts (proposeQuery, getApprovalState, getQueryProposal)
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useQueryProposalStore = defineStore('query-proposal', () => {
  // ── State ──

  const proposedQuery = ref(null)
  // Shape: { template, concepts, roles, dag, confidence, country_code }

  const approvalState = ref('idle')
  // 'idle' | 'pending' | 'approved' | 'edited' | 'rejected'

  const userEdits = ref(null)
  // Shape: [{ type, text }] — edited concept values

  const executionResult = ref(null)
  // Shape: { answer, results, trace, latency_ms }

  // ── Computed ──

  const hasPendingProposal = computed(
    () => approvalState.value === 'pending'
  )

  const isResolved = computed(
    () => ['approved', 'edited', 'rejected'].includes(approvalState.value)
  )

  // ── Actions ──

  function proposeQuery(query) {
    proposedQuery.value = query
    approvalState.value = 'pending'
    userEdits.value = null
    executionResult.value = null
  }

  function approveQuery() {
    approvalState.value = 'approved'
  }

  function editQuery(editedConcepts) {
    userEdits.value = editedConcepts
    if (proposedQuery.value) {
      proposedQuery.value = {
        ...proposedQuery.value,
        concepts: editedConcepts,
      }
    }
    approvalState.value = 'edited'
  }

  function rejectQuery() {
    approvalState.value = 'rejected'
  }

  function setExecutionResult(result) {
    executionResult.value = result
  }

  function reset() {
    proposedQuery.value = null
    approvalState.value = 'idle'
    userEdits.value = null
    executionResult.value = null
  }

  return {
    // State
    proposedQuery,
    approvalState,
    userEdits,
    executionResult,
    // Computed
    hasPendingProposal,
    isResolved,
    // Actions
    proposeQuery,
    approveQuery,
    editQuery,
    rejectQuery,
    setExecutionResult,
    reset,
  }
})
