<template>
  <div class="login d-flex align-items-center justify-content-center h-100">
    <div class="card login-card">
      <div class="card-body p-4">
        <h1 class="fs-4 fw-bold mb-1">World KG &amp; Geo Spatial Reasoning</h1>
        <p class="text-secondary small mb-3">Sign in to continue</p>

        <div class="btn-group w-100 mb-3" role="group">
          <button
            type="button"
            class="btn btn-sm"
            :class="mode === 'login' ? 'btn-primary' : 'btn-outline-secondary'"
            @click="switchMode('login')"
          >
            Sign in
          </button>
          <button
            type="button"
            class="btn btn-sm"
            :class="mode === 'register' ? 'btn-primary' : 'btn-outline-secondary'"
            @click="switchMode('register')"
          >
            Create account
          </button>
        </div>

        <form @submit.prevent="submit">
          <div v-if="mode === 'register'" class="mb-3">
            <label class="form-label small" for="inviteCode">Invite code</label>
            <input id="inviteCode" v-model="inviteCode" type="text" class="form-control" autocomplete="off" required />
          </div>
          <div class="mb-3">
            <label class="form-label small" for="username">Username</label>
            <input id="username" v-model="username" type="text" class="form-control" autocomplete="username" required />
          </div>
          <div v-if="mode === 'register'" class="mb-3">
            <label class="form-label small" for="email">Email (optional)</label>
            <input id="email" v-model="email" type="email" class="form-control" autocomplete="email" />
          </div>
          <div class="mb-3">
            <label class="form-label small" for="password">Password</label>
            <input id="password" v-model="password" type="password" class="form-control" autocomplete="current-password" required />
          </div>

          <div v-if="error" class="alert alert-danger small py-2 mb-3">{{ error }}</div>

          <button type="submit" class="btn btn-primary w-100" :disabled="busy">
            {{ busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account' }}
          </button>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/authStore'

const router = useRouter()
const auth = useAuthStore()

const mode = ref('login') // 'login' | 'register'
const username = ref('')
const password = ref('')
const inviteCode = ref('')
const email = ref('')
const error = ref('')
const busy = ref(false)

function switchMode(m) {
  mode.value = m
  error.value = ''
}

async function submit() {
  error.value = ''
  busy.value = true
  try {
    if (mode.value === 'login') {
      await auth.login(username.value, password.value)
    } else {
      await auth.register(inviteCode.value, username.value, password.value, email.value)
    }
    router.push('/')
  } catch (e) {
    error.value = e.response?.data?.detail || 'Something went wrong. Try again.'
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.login {
  background: #0f172a;
}
.login-card {
  width: 100%;
  max-width: 380px;
  background: #1e293b;
  border: 1px solid #334155;
}
</style>
