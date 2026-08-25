import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'
import AgentPlayground from '../views/AgentPlayground.vue'

const routes = [
  {
    path: '/',
    name: 'home',
    component: Home,
  },
  {
    path: '/agent',
    name: 'agent',
    component: AgentPlayground,
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
