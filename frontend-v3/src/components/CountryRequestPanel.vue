<template>
  <!-- CountryRequestPanel (2026-10-16): shown when an unprocessed
       country is picked — the request view replaces the tool surface
       entirely. Captcha-gated POST to /api/country-requests/ feeds the
       per-country tally (CountryRequest table). -->
  <div class="d-flex flex-column gap-2">
    <h2 class="fs-6 fw-semibold mb-0">{{ displayName }} isn't processed yet</h2>
    <p class="small text-secondary mb-0">
      This country has no pipeline data yet, so the tools stay hidden
      here. Request processing and we will queue it.
    </p>

    <template v-if="submitted">
      <div class="alert alert-success small py-1 px-2 mb-0">
        Request counted — {{ displayName }} now has
        {{ totalRequests }} request{{ totalRequests === 1 ? '' : 's' }}.
      </div>
    </template>
    <form v-else class="d-flex flex-column gap-2" @submit.prevent="submit">
      <input
        v-model="name"
        type="text"
        class="form-control form-control-sm"
        placeholder="Name (optional)"
        maxlength="255"
      />
      <div class="d-flex align-items-center gap-2">
        <label class="small text-secondary mb-0 text-nowrap" for="cr-captcha">
          {{ captcha.question || '…' }}
        </label>
        <input
          id="cr-captcha"
          v-model="answer"
          type="text"
          inputmode="numeric"
          class="form-control form-control-sm"
          style="width: 5rem;"
          required
        />
      </div>
      <div v-if="error" class="small text-danger">{{ error }}</div>
      <div>
        <button type="submit" class="btn btn-sm btn-primary" :disabled="submitting">
          {{ submitting ? 'Requesting…' : 'Request processing' }}
        </button>
      </div>
    </form>

    <button
      class="btn btn-link btn-sm text-secondary p-0 small text-start"
      style="width: fit-content;"
      @click="$emit('pick-another')"
    >Pick another country</button>
  </div>
</template>

<script>
import axios from 'axios'

export default {
  name: 'CountryRequestPanel',
  props: {
    country: { type: Object, required: true },
    /** 'tools' | 'doppelganger' — which surface the request came from. */
    surface: { type: String, default: '' },
  },
  emits: ['pick-another'],
  data() {
    return {
      captcha: {},
      name: '',
      answer: '',
      submitting: false,
      submitted: false,
      totalRequests: null,
      error: '',
    }
  },
  computed: {
    displayName() {
      const n = this.country.name || ''
      return n.charAt(0).toUpperCase() + n.slice(1)
    },
  },
  mounted() {
    this.refreshCaptcha()
  },
  methods: {
    async refreshCaptcha() {
      try {
        const { data } = await axios.get('/country-requests/captcha/')
        this.captcha = data
        this.answer = ''
      } catch {
        this.error = 'Could not load captcha.'
      }
    },
    async submit() {
      this.submitting = true
      this.error = ''
      try {
        const { data } = await axios.post('/country-requests/', {
          country_name: this.country.name,
          iso_code: this.country.iso_code || '',
          continent: this.country.continent || '',
          surface: this.surface,
          name: this.name,
          captcha_id: this.captcha.captcha_id,
          captcha_answer: this.answer,
        })
        this.submitted = true
        this.totalRequests = data.total_requests
      } catch (err) {
        this.error = err.response?.data?.error || 'Request failed.'
        this.refreshCaptcha()
      } finally {
        this.submitting = false
      }
    },
  },
}
</script>
