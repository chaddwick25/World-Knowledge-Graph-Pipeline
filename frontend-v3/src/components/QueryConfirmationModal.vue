<template>
  <Teleport to="body">
    <div v-if="queryProposalStore.hasPendingProposal" class="qcm-backdrop">
      <div class="qcm-modal">
        <header class="qcm-modal__header">
          <h3 class="qcm-modal__title">Confirm Query</h3>
          <button
            class="qcm-modal__close"
            @click="queryProposalStore.rejectQuery"
          >
            &times;
          </button>
        </header>

        <section v-if="queryProposalStore.proposedQuery" class="qcm-modal__body">
          <!-- Template + confidence -->
          <div class="qcm-modal__template">
            <span class="qcm-modal__template-name">
              {{ queryProposalStore.proposedQuery.template }}
            </span>
            <span
              class="qcm-modal__confidence"
              :class="confidenceClass"
            >
              {{ (queryProposalStore.proposedQuery.confidence * 100).toFixed(0) }}% confident
            </span>
          </div>

          <!-- Editable concept slots -->
          <div class="qcm-modal__concepts">
            <div
              v-for="(concept, idx) in editableConcepts"
              :key="idx"
              class="qcm-modal__concept"
            >
              <span class="qcm-modal__concept-type">{{ concept.type }}</span>
              <span
                v-if="concept.role"
                class="qcm-modal__concept-role"
              >
                {{ concept.role }}
              </span>
              <input
                v-model="editableConcepts[idx].text"
                class="qcm-modal__concept-input"
                :placeholder="concept.text || 'Not extracted'"
              />
            </div>
          </div>

          <!-- Natural language summary -->
          <div class="qcm-modal__summary">
            <strong>I understood:</strong>
            {{ summaryText }}
          </div>
        </section>

        <footer class="qcm-modal__footer">
          <button
            class="qcm-modal__btn qcm-modal__btn--reject"
            @click="queryProposalStore.rejectQuery"
          >
            Reject
          </button>
          <button
            class="qcm-modal__btn qcm-modal__btn--edit"
            @click="saveEdits"
          >
            Save Edits &amp; Approve
          </button>
          <button
            class="qcm-modal__btn qcm-modal__btn--approve"
            @click="queryProposalStore.approveQuery"
          >
            Approve
          </button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<script>
/**
 * QueryConfirmationModal — HITL confirmation for MapQA parsed queries.
 *
 * Shows the agent-proposed query as editable concept slots. The user can:
 *   - Approve: concepts are correct → proceed
 *   - Edit: fix extracted text (e.g. "Hollywood Blvd" → "Hollywood Boulevard")
 *   - Reject: parser got it wrong → user rephrases or gives up
 *
 * The modal is driven by the queryProposalStore Pinia store, which is
 * populated by the MCP `proposeQuery` tool (called by the agent).
 */
import { computed, watch, ref } from 'vue'
import { useQueryProposalStore } from '../stores/queryProposalStore'

export default {
  name: 'QueryConfirmationModal',
  setup() {
    const queryProposalStore = useQueryProposalStore()

    // Local editable copy of concepts (initialized when a proposal arrives)
    const editableConcepts = ref([])

    // Watch for new proposals and initialize editable concepts
    watch(
      () => queryProposalStore.proposedQuery,
      (proposal) => {
        if (proposal && proposal.concepts) {
          // Merge concept + role info
          const roleMap = {}
          if (proposal.roles) {
            for (const r of proposal.roles) {
              roleMap[r.concept_type] = r.role
            }
          }
          editableConcepts.value = proposal.concepts.map((c) => ({
            type: c.type,
            text: c.text || '',
            role: roleMap[c.type] || null,
          }))
        }
      },
      { immediate: true }
    )

    const confidenceClass = computed(() => {
      const c = queryProposalStore.proposedQuery?.confidence || 0
      if (c >= 0.8) return 'qcm-modal__confidence--high'
      if (c >= 0.6) return 'qcm-modal__confidence--medium'
      return 'qcm-modal__confidence--low'
    })

    const summaryText = computed(() => {
      const p = queryProposalStore.proposedQuery
      if (!p) return ''
      const parts = []
      for (const c of editableConcepts.value) {
        if (c.text) {
          parts.push(`${c.type.toLowerCase()}: "${c.text}"`)
        }
      }
      if (parts.length === 0) return 'No concepts extracted.'
      return `you're looking for ${parts.join(', ')}.`
    })

    function saveEdits() {
      queryProposalStore.editQuery(
        editableConcepts.value.map((c) => ({
          type: c.type,
          text: c.text,
        }))
      )
    }

    return {
      queryProposalStore,
      editableConcepts,
      confidenceClass,
      summaryText,
      saveEdits,
    }
  },
}
</script>

<style scoped>
.qcm-backdrop {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}

.qcm-modal {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 0.75rem;
  width: 90%;
  max-width: 540px;
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
}

.qcm-modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1rem 1.25rem;
  border-bottom: 1px solid #1e293b;
}

.qcm-modal__title {
  margin: 0;
  font-size: 1.1rem;
  font-weight: 600;
  color: #f1f5f9;
}

.qcm-modal__close {
  background: none;
  border: none;
  color: #94a3b8;
  font-size: 1.5rem;
  cursor: pointer;
  padding: 0;
  line-height: 1;
}

.qcm-modal__close:hover {
  color: #f1f5f9;
}

.qcm-modal__body {
  padding: 1rem 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.qcm-modal__template {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.qcm-modal__template-name {
  font-size: 0.85rem;
  font-weight: 600;
  color: #a5b4fc;
}

.qcm-modal__confidence {
  font-size: 0.72rem;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
}

.qcm-modal__confidence--high {
  background: #064e3b;
  color: #6ee7b7;
}

.qcm-modal__confidence--medium {
  background: #78350f;
  color: #fcd34d;
}

.qcm-modal__confidence--low {
  background: #7f1d1d;
  color: #fca5a5;
}

.qcm-modal__concepts {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.qcm-modal__concept {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.qcm-modal__concept-type {
  min-width: 80px;
  padding: 0.2rem 0.5rem;
  border-radius: 0.3rem;
  background: #1e293b;
  color: #93c5fd;
  font-size: 0.75rem;
  font-weight: 600;
  text-align: center;
}

.qcm-modal__concept-role {
  min-width: 70px;
  padding: 0.15rem 0.4rem;
  border-radius: 0.3rem;
  background: #312e81;
  color: #c7d2fe;
  font-size: 0.68rem;
  font-weight: 600;
  text-align: center;
}

.qcm-modal__concept-input {
  flex: 1;
  padding: 0.35rem 0.6rem;
  border-radius: 0.4rem;
  border: 1px solid #334155;
  background: #020617;
  color: #e2e8f0;
  font-size: 0.8rem;
}

.qcm-modal__concept-input:focus {
  outline: none;
  border-color: #6366f1;
}

.qcm-modal__summary {
  padding: 0.6rem 0.75rem;
  border-radius: 0.4rem;
  background: #1e293b;
  font-size: 0.8rem;
  color: #cbd5e1;
}

.qcm-modal__summary strong {
  color: #f1f5f9;
}

.qcm-modal__footer {
  display: flex;
  gap: 0.5rem;
  padding: 1rem 1.25rem;
  border-top: 1px solid #1e293b;
  justify-content: flex-end;
}

.qcm-modal__btn {
  padding: 0.4rem 1rem;
  border-radius: 0.5rem;
  border: none;
  font-size: 0.82rem;
  cursor: pointer;
  font-weight: 500;
}

.qcm-modal__btn--reject {
  background: transparent;
  border: 1px solid #475569;
  color: #94a3b8;
}

.qcm-modal__btn--reject:hover {
  background: #1e293b;
  color: #f1f5f9;
}

.qcm-modal__btn--edit {
  background: #1e293b;
  color: #c7d2fe;
}

.qcm-modal__btn--edit:hover {
  background: #312e81;
}

.qcm-modal__btn--approve {
  background: #4f46e5;
  color: #f9fafb;
}

.qcm-modal__btn--approve:hover {
  background: #4338ca;
}
</style>
