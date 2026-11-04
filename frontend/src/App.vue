<template>
  <div class="app-container">
    <!-- 顶栏 -->
    <header class="topbar">
      <div class="brand" @click="goShelf">
        <span class="brand-icon">📖</span>
        <div>
          <h1>小说角色扮演</h1>
          <p>上传小说，把书里的人变成能和你说话的角色</p>
        </div>
      </div>

      <div class="topbar-actions">
        <label class="name-field">
          <span>我是</span>
          <input v-model="myName" @change="onNameChange" maxlength="16" placeholder="读者" />
        </label>
      </div>
    </header>

    <!-- 后端未配置 Key 的提示 -->
    <div v-if="health && health.api_key_configured === false" class="banner warn">
      后端还没有配置 DEEPSEEK_API_KEY，上传后的角色抽取与聊天都会失败。在
      <code>backend/.env</code> 里填好后重启后端即可。
    </div>
    <p v-if="error" class="banner error">{{ error }}</p>

    <div v-if="health?.rag" class="banner rag">
      🧠 记忆库：{{ health.rag.backend === 'vector' ? '语义向量' : '词法检索' }}
      <span v-if="health.rag.backend !== 'vector' && !health.rag.model_exists" class="rag-hint">
        （模型未就绪，角色记忆暂时按关键词检索）
      </span>
      <span v-else-if="health.rag.backend === 'vector'" class="rag-hint">
        （{{ health.rag.model_path.split(/[\\/]/).pop() }}）
      </span>
    </div>

    <!-- 视图：书架 / 群聊 / 群聊信息 / 单聊 -->
    <ShelfPage v-if="view === 'shelf'" />
    <GroupChatPage v-else-if="view === 'group'" />
    <GroupInfoPage v-else-if="view === 'groupInfo'" />
    <CharacterChatPage v-else />

    <!-- 浮层 -->
    <MergePicker />
    <CharacterEditor />
    <CharacterStatus />
  </div>
</template>

<script setup>
import { onMounted, onUnmounted } from 'vue'

import CharacterStatus from './components/modals/CharacterStatus.vue'
import CharacterEditor from './components/modals/CharacterEditor.vue'
import MergePicker from './components/modals/MergePicker.vue'
import CharacterChatPage from './pages/CharacterChatPage.vue'
import GroupChatPage from './pages/GroupChatPage.vue'
import GroupInfoPage from './pages/GroupInfoPage.vue'
import ShelfPage from './pages/ShelfPage.vue'
import { useApp } from './stores/app.js'

// 外壳只负责：顶栏 + 按 view 切页面 + 挂浮层
const { view, myName, health, error, onNameChange, goShelf, bootstrap, stopPolling } = useApp()

onMounted(bootstrap)
onUnmounted(stopPolling)
</script>
