<!-- Execution trace — decision flow (each step shows what it decided).
     Colors follow the AI answer palette: green (success) for steps on
     the answer path, red (danger) for warnings/errors. The caret is
     red while closed, gray once fully open.

     Leaf component (layer 3): receives the decorated trace nodes from
     SemanticSearchPanel and renders them. Extracted from
     SemanticSearchPanel.vue (monolith split, Phase 5). -->
<template>
  <div v-if="nodes.length > 0" class="mt-1 small text-secondary">
    <details class="trace-details">
      <summary class="cursor-pointer">Execution trace ({{ nodes.length + 1 }} steps)</summary>
      <div class="trace-flow mt-1">
        <!-- WK Template headline: first node in the trace. Template name
             + confidence badge. "WK Template" links to the ACL paper. -->
        <div v-if="parsedQuery" class="trace-node">
          <div class="d-flex align-items-baseline gap-2">
            <span class="trace-icon bg-primary-subtle" aria-hidden="true">
              <i class="bi bi-file-earmark-text text-primary"></i>
            </span>
            <a
              href="https://aclanthology.org/2026.acl-long.679.pdf"
              target="_blank"
              rel="noopener"
              class="trace-link fw-semibold"
            >WK Template</a>
            <span class="text-secondary">: {{ parsedQuery.template }}</span>
            <span
              class="badge rounded-pill"
              :class="confidenceBadgeClass"
            >
              {{ (parsedQuery.confidence * 100).toFixed(0) }}% confident
            </span>
          </div>
        </div>
        <div
          v-for="(node, idx) in nodes"
          :key="idx"
          :class="['trace-node', node.status ? `trace-node--${node.status}` : '']"
        >
          <div class="d-flex align-items-baseline gap-2">
            <span
              :class="['trace-icon', node.iconBg, node.live ? 'trace-icon--live' : '']"
              aria-hidden="true"
            >
              <i :class="['bi', node.icon, node.iconColor]"></i>
            </span>
            <strong :class="node.color ? `text-${node.color}` : ''">{{ node.title }}</strong>
            <!-- Status protocol: red = error, amber = warning/degraded -->
            <span
              v-if="node.status === 'error'"
              class="trace-status trace-status--error"
              title="error"
            >
              <i class="bi bi-x-octagon-fill"></i>
            </span>
            <span
              v-else-if="node.status === 'warning'"
              class="trace-status trace-status--warning"
              title="warning"
            >
              <i class="bi bi-exclamation-triangle-fill"></i>
            </span>
            <span v-if="node.error" class="text-danger">— {{ node.error }}</span>
          </div>
          <ul v-if="node.lines.length" class="trace-lines">
            <li v-for="(line, li) in node.lines" :key="li">
              <!-- Segmented line: links (entity, WorldKG class, OSM tag)
                   are green, underlined on hover, open in a new tab. -->
              <template v-if="Array.isArray(line?.segments)">
                <template v-for="(seg, si) in line.segments" :key="si">
                  <template v-if="si > 0">{{ line.sep || ' · ' }}</template>
                  <a
                    v-if="seg && seg.href"
                    :href="seg.href"
                    target="_blank"
                    rel="noopener"
                    class="trace-link"
                  >{{ seg.text }}</a>
                  <template v-else>{{ seg }}</template>
                </template>
              </template>
              <template v-else>{{ line }}</template>
            </li>
          </ul>
        </div>
      </div>
    </details>
  </div>
</template>

<script>
export default {
  name: 'ExecutionTraceFlow',
  props: {
    /** Decorated trace nodes (SemanticSearchPanel.traceFlow). */
    nodes: { type: Array, default: () => [] },
    /** Parsed query for the WK Template headline node. */
    parsedQuery: { type: Object, default: null },
    /** Bootstrap badge class for the confidence pill. */
    confidenceBadgeClass: { type: String, default: 'text-bg-secondary' },
  },
}
</script>

<style scoped>
/* Execution trace decision flow: vertical spine with dashed separators */
/* Execution trace decision flow: vertical spine with dashed separators
   between nodes so each step reads as one decision in the chain. When
   open, the spine matches the AI answer box's green border. */
.trace-flow {
  border-left: 2px solid var(--bs-secondary-border-subtle);
  padding-left: 0.75rem;
}
.trace-details[open] .trace-flow {
  border-left-color: var(--bs-success-border-subtle);
}

/* Trace caret: the native disclosure triangle, red while closed
   (attention), gray once fully open. */
.trace-details > summary::marker {
  color: var(--bs-danger);
}
.trace-details > summary::-webkit-details-marker {
  color: var(--bs-danger);
}
.trace-details[open] > summary::marker {
  color: var(--bs-secondary);
}
.trace-details[open] > summary::-webkit-details-marker {
  color: var(--bs-secondary);
}
.trace-node + .trace-node {
  margin-top: 0.5rem;
  padding-top: 0.5rem;
  border-top: 1px dashed var(--bs-secondary-border-subtle);
}

/* Status protocol: red = error, amber = warning/degraded. A left accent
   bar + a badge icon flag the node without recoloring the whole line. */
.trace-node {
  padding-left: 0.4rem;
}
.trace-node--error {
  border-left: 3px solid var(--bs-danger);
}
.trace-node--warning {
  border-left: 3px solid var(--bs-warning);
}
.trace-status {
  display: inline-flex;
  align-items: center;
  line-height: 1;
}
.trace-status i {
  font-size: 0.85rem;
}
.trace-status--error {
  color: var(--bs-danger);
}
.trace-status--warning {
  color: var(--bs-warning);
}
.trace-lines {
  margin: 0.15rem 0 0;
  padding-left: 1.35rem;
  list-style: none;
}

/* Icon chip: subtle tinted background so each node's icon has presence. */
.trace-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.35rem;
  height: 1.35rem;
  border-radius: 0.3rem;
  flex-shrink: 0;
}
.trace-icon i {
  font-size: 0.8rem;
  line-height: 1;
}

/* Live pulse for context-assembly nodes (entity_context). */
@keyframes trace-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.55;
  }
}
.trace-icon--live {
  animation: trace-pulse 2.2s ease-in-out infinite;
}

/* Entity link: green, underline on hover, opens in a new tab. */
.trace-link {
  color: var(--bs-success);
  text-decoration: none;
}
.trace-link:hover {
  color: var(--bs-success);
  text-decoration: underline;
}
</style>
