import { createApp } from "vue"
import "bootstrap/dist/css/bootstrap.css"
import "bootstrap-vue-next/dist/bootstrap-vue-next.css"
import "bootstrap-icons/font/bootstrap-icons.css"
import "./styles/theme.css"
import App from "./App.vue"
import router from "./router"
import { createPinia } from "pinia"
import { createBootstrap } from "bootstrap-vue-next/plugins/createBootstrap"
import axios from "axios"

// Match the existing frontend: point axios at the Django API.
// VITE_API_BASE_URL overrides it at build time. Same-origin self-host:
// set it to '/api' so REST, SSE (derived from axios.defaults.baseURL) and
// the WebSocket (derived in pipelineStore) all stay on one host.
axios.defaults.baseURL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api'

function getCookie(name) {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'))
  return m ? decodeURIComponent(m[1]) : null
}

// Session auth (Django): echo the csrftoken cookie on unsafe requests.
axios.interceptors.request.use((config) => {
  const token = getCookie('csrftoken')
  if (token && !/^(GET|HEAD|OPTIONS)$/i.test(config.method || 'GET')) {
    config.headers['X-CSRFToken'] = token
  }
  return config
})

// Login guard on 401s (except for the login page itself).
axios.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401 && router.currentRoute.value.name !== 'login') {
      router.push({ name: 'login' })
    }
    return Promise.reject(err)
  }
)

// Activate Bootstrap 5.3 dark mode so theme.css var overrides apply.
document.documentElement.setAttribute('data-bs-theme', 'dark')

const app = createApp(App)
app.use(router)
app.use(createPinia())
app.use(createBootstrap())
app.mount('#app')
