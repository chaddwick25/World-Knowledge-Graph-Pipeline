import { createApp } from "vue"
import "bootstrap/dist/css/bootstrap.css"
import "bootstrap-vue-next/dist/bootstrap-vue-next.css"
import "./style.css"
import App from "./App.vue"
import router from "./router"
import { createPinia } from "pinia"
import { createBootstrap } from "bootstrap-vue-next/plugins/createBootstrap"
import axios from "axios"

// Match the existing frontend: point axios at the Django API.
axios.defaults.baseURL = 'http://localhost:8000/api'

const app = createApp(App)
app.use(router)
app.use(createPinia())
app.use(createBootstrap())
app.mount('#app')
