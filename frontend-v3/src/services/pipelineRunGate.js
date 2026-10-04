// ── Pipeline-run gate (extracted from Home.vue, 2026-10-02) ────────────────
// The run sequence is a gate chain: completed-date confirm → password gate
// (when VITE_PIPELINE_PASSWORD is configured) → store.startPipeline →
// completion side effects. `self` is the Home component instance — these
// functions mutate its data fields and are bound via one-line delegates.

import { usePipelineStore } from '../stores/pipelineStore'

export function handleRunPipeline(self) {
  if (!self.singleCountry || self.isPipelineRunning) return
  if (!self.singleCountry.is_geovectors_supported) {
    self.errorMessage = 'No embeddings available for this country — pipeline cannot run'
    self.pipelineDoneStatus = 'failed'
    return
  }

  // If the selected date is already completed, confirm via modal
  const isCompleted = self.storeUsedDates.has(self.selectedSnapshotDate)
  if (isCompleted) {
    self.rerunConfirmDate = self.selectedSnapshotDate
    self.showRerunConfirm = true
    return
  }

  requestPassword(self, false)
}

export function confirmRerun(self) {
  self.showRerunConfirm = false
  requestPassword(self, true)
}

function requestPassword(self, force) {
  const expected = import.meta.env.VITE_PIPELINE_PASSWORD
  if (!expected) {
    startPipelineRun(self, force)
    return
  }
  self.pendingPasswordForce = force
  self.pipelinePasswordError = ''
  self.showPipelinePassword = true
}

export function onPipelinePasswordSubmit(self, password) {
  const expected = (import.meta.env.VITE_PIPELINE_PASSWORD || '').trim()
  if (password !== expected) {
    self.pipelinePasswordError = 'Incorrect password'
    return
  }
  self.pipelinePasswordError = ''
  self.showPipelinePassword = false
  startPipelineRun(self, self.pendingPasswordForce)
}

async function startPipelineRun(self, force) {
  self.isPipelineRunning = true
  self.pipelineDoneStatus = null
  self.errorMessage = ''
  const store = usePipelineStore()
  try {
    const sessionId = await store.startPipeline(
      self.singleCountry.name,
      self.selectedSnapshotDate,
      { force }
    )
    self.pipelineSessionId = sessionId || null
  } catch (err) {
    const status = err.response?.status
    const reason = err.response?.data?.error || err.message || 'Failed to start pipeline'
    self.errorMessage = reason
    self.isPipelineRunning = false
    if (status === 409) {
      self.pipelineDoneStatus = 'skipped'
      // Refresh DB-backed snapshot jobs so the year badge updates.
      if (self.singleCountry?.iso_code) {
        await store.fetchSnapshotJobs(
          self.singleCountry.name,
          self.singleCountry.iso_code,
        )
      }
    } else {
      self.pipelineDoneStatus = 'failed'
    }
  }
}

export function onPipelineDone(self, event) {
  self.isPipelineRunning = false
  self.pipelineDoneStatus = event.status || 'failed'

  if (event.status === 'completed') {
    // Refresh country status to see new data
    self.fetchCountryStatus()
    // Refresh DB-backed snapshot jobs so the year badge updates
    if (self.singleCountry?.iso_code) {
      const store = usePipelineStore()
      store.fetchSnapshotJobs(
        self.singleCountry.name,
        self.singleCountry.iso_code,
      )
    }
  }
}
