import { createPinia, setActivePinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { router } from './router'
import { useLocalSessionStore } from './stores/local-session'
import 'element-plus/es/components/tabs/style/css'
import './styles/main.scss'

const pinia = createPinia()
setActivePinia(pinia)
useLocalSessionStore().restore()

createApp(App).use(pinia).use(router).mount('#app')
