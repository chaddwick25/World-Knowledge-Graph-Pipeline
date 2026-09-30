import { defineStore } from 'pinia'
import axios from 'axios'

// Session-auth store. The session cookie is the source of truth; on app
// load, init() obtains the CSRF token and restores the user via /auth/me/.
export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null,
    ready: false,
  }),

  actions: {
    async init() {
      try {
        await axios.get('/auth/csrf/')
      } catch (e) {
        // Non-fatal — the login page still works.
      }
      await this.fetchMe()
    },

    async fetchMe() {
      try {
        const { data } = await axios.get('/auth/me/')
        this.user = data.user
      } catch (e) {
        this.user = null
      } finally {
        this.ready = true
      }
    },

    async login(username, password) {
      const { data } = await axios.post('/auth/login/', { username, password })
      this.user = data.user
    },

    async register(inviteCode, username, password, email) {
      const { data } = await axios.post('/auth/register/', {
        invite_code: inviteCode,
        username,
        password,
        email,
      })
      this.user = data.user
    },

    async logout() {
      try {
        await axios.post('/auth/logout/')
      } finally {
        this.user = null
      }
    },
  },
})
