import { createRouter, createWebHistory } from 'vue-router'
import HomeV2 from '../views/HomeV2.vue'

const routes = [
  {
    path: '/',
    name: 'home',
    component: HomeV2,
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
