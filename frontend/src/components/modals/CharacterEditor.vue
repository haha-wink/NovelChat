<template>
    <!-- ============ 角色编辑弹窗 ============ -->
    <div v-if="editor.open" class="modal-mask" @click.self="closeEditor">
      <div class="modal">
        <h3>{{ editor.mode === 'create' ? '添加角色' : `编辑「${editor.form.name}」` }}</h3>
        <p class="sub">这些字段会直接变成角色的扮演依据（初始记忆）。</p>

        <div class="form-grid">
          <label>
            <span>名字 *</span>
            <input v-model="editor.form.name" maxlength="32" placeholder="如：沈砚 / 我" />
          </label>
          <label>
            <span>身份</span>
            <input v-model="editor.form.role" maxlength="60" placeholder="如：剑客 / 女主角" />
          </label>
          <label class="wide">
            <span>别名 / 称呼（用逗号或顿号分隔，用于识别「同一个人」）</span>
            <input v-model="editor.form.aliasesText" placeholder="如：诺诺、大小姐" />
          </label>
          <label class="wide">
            <span>性格（用逗号或顿号分隔）</span>
            <input v-model="editor.form.personalityText" placeholder="寡言、冷峻、心软" />
          </label>
          <label class="wide">
            <span>说话风格</span>
            <input v-model="editor.form.speech_style" maxlength="300" placeholder="句子很短，几乎不解释" />
          </label>
          <label class="wide">
            <span>背景设定</span>
            <textarea v-model="editor.form.background" rows="3" placeholder="身份来历、和谁有什么关系…"></textarea>
          </label>
          <label v-if="editor.mode === 'create'">
            <span>头像表情</span>
            <input v-model="editor.form.avatar" maxlength="4" placeholder="🗡️" />
          </label>
          <label v-if="editor.mode === 'create'" class="check">
            <input type="checkbox" v-model="editor.form.is_user_persona" />
            <span>这是我自己的化身</span>
          </label>
        </div>

        <!-- 头像：可让后端联网抓候选，点一张即选中 -->
        <div class="avatar-picker">
          <div class="avatar-picker-head">
            <span>头像</span>
            <button
              v-if="editor.mode === 'edit'"
              class="ghost small"
              :disabled="editor.crawling"
              @click="crawlAvatars"
            >
              {{ editor.crawling ? '正在联网查找…' : '🔍 从网上找头像' }}
            </button>
          </div>

          <p v-if="editor.avatarError" class="error-text">{{ editor.avatarError }}</p>

          <div v-if="editor.candidates.length" class="avatar-grid">
            <button
              v-for="item in editor.candidates"
              :key="item.url"
              :class="['avatar-choice', { active: editor.form.avatar_url === item.url }]"
              :title="`来源：${item.source}`"
              @click="editor.form.avatar_url = item.url"
            >
              <img :src="avatarSrc(item.url)" :alt="item.source" loading="lazy" />
            </button>
            <button
              class="avatar-choice clear"
              :class="{ active: !editor.form.avatar_url }"
              title="不用图片，改用 emoji"
              @click="editor.form.avatar_url = ''"
            >
              用 emoji
            </button>
          </div>
          <p v-else-if="!editor.crawling" class="sub">
            没有候选就用 emoji 头像；点上面的按钮可以让后端去 Bangumi / 萌娘百科 / 维基找图。
          </p>
        </div>

        <p v-if="editor.error" class="error-text">{{ editor.error }}</p>

        <div class="modal-actions">
          <button class="ghost" @click="closeEditor">取消</button>
          <button class="primary" :disabled="editor.saving" @click="saveCharacter">
            {{ editor.saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
</template>

<script setup>
import { useApp } from '../../stores/app.js'

// 一次拿全部应用状态与动作；模板里直接写名字
const {
  MAX_IMAGES,
  MAX_IMAGE_BYTES,
  NAME_KEY,
  LAST_SHELF_KEY,
  view,
  health,
  error,
  myName,
  books,
  loadingBooks,
  uploading,
  activeBook,
  characters,
  activeCharacter,
  chatInput,
  isLoading,
  chatContainer,
  textareaRef,
  pendingImages,
  conversations,
  editor,
  status,
  group,
  groupContainer,
  shelves,
  uploadShelfId,
  groupInfo,
  mergePanel,
  MEMBER_COLS,
  MEMBER_ROWS,
  merge,
  verifying,
  currentMessages,
  loadHealth,
  loadBooks,
  booksOfShelf,
  schedulePoll,
  handleUpload,
  retryBook,
  removeBook,
  createShelf,
  renameShelf,
  removeShelf,
  openShelfChat,
  enterGroup,
  openBook,
  goShelf,
  backToGroup,
  openChat,
  loadChatHistory,
  newChat,
  removeCharacter,
  openEditor,
  closeEditor,
  closeStatus,
  openStatus,
  openMemberStatus,
  mergeCandidates,
  openMergePicker,
  confirmMerge,
  unmergeChild,
  toggleMemberStatus,
  crawlAvatars,
  verifyBook,
  parsePersonality,
  saveCharacter,
  tokenStorageKey,
  onNameChange,
  ensureToken,
  refreshToken,
  handleKeydown,
  handleFileSelect,
  scrollToBottom,
  sendMessage,
  fetchChat,
  readStream,
  handleGroupKeydown,
  scrollGroupBottom,
  groupTokenKey,
  ensureGroupToken,
  loadGroupMembers,
  openGroup,
  loadGroupHistory,
  turnToGroupMessage,
  clearGroupHistory,
  openGroupInfo,
  loadMergeCandidates,
  resolveMerge,
  closeGroupInfo,
  visibleMembers,
  hasMoreMembers,
  saveGroupTitle,
  toggleMute,
  parseLocalMentions,
  sendGroupMessage,
  readGroupStream,  avatarSrc,
  formatCount,
  formatTime,
  groupMentionText,
  memberStateText,
  statusText,
  renderMarkdown,
} = useApp()
</script>
