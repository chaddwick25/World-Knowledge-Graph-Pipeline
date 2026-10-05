// ── Pipeline-run gate (extracted from Home.vue, 2026-10-02) ────────────────
// The run sequence is a gate chain: completed-date confirm → store.startPipeline
// → completion side effects. The operator pipeline key is checked SERVER-side
// (settings.PIPELINE_TRIGGER_KEY, env-only); the SPA never ships it. When the
// backend 403s (key required/mismatch), the modal collects it and retries.
// `self` is the Home component instance — these functions mutate its data
// fields and are bound via one-line delegates.

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

  startPipelineRun(self, false)
}

export function confirmRerun(self) {
  self.showRerunConfirm = false
  startPipelineRun(self, true)
}

export function onPipelinePasswordSubmit(self, password) {
  self.showPipelinePassword = false
  self.pipelinePasswordError = ''
  startPipelineRun(self, self.pendingPasswordForce, (password || '').trim())
}

async function startPipelineRun(self, force, pipelineKey = '') {
  self.isPipelineRunning = true
  self.pipelineDoneStatus = null
  self.errorMessage = ''
  const store = usePipelineStore()
  try {
    const sessionId = await store.startPipeline(
      self.singleCountry.name,
      self.selectedSnapshotDate,
      { force, pipelineKey },
    )
    self.pipelineSessionId = sessionId || null
  } catch (err) {
    const status = err.response?.status
    const reason = err.response?.data?.error || err.message || 'Failed to start pipeline'
    self.isPipelineRunning = false

    if (status === 403) {
      // Operator key required (or wrong) — collect it and retry.
      self.pendingPasswordForce = force
      self.pipelinePasswordError = self.showPipelinePassword ? 'Incorrect pipeline key' : ''
      self.showPipelinePassword = true
      return
    }

    self.errorMessage = reason
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
