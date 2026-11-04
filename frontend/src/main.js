import './styles/app.css'
import { createApp } from 'vue'
import App from './App.vue'
import './style.css' // 如果你需要全局样式，可以建这个文件，不需要则删掉这行

createApp(App).mount('#app')