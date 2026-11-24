import { createRouter, createWebHistory } from 'vue-router'

import AttributionReview from '@/views/AttributionReview.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/attribution-review', name: 'attribution-review', component: AttributionReview },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
