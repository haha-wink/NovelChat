<template>
    <!-- ============ 角色详情 / 状态 ============ -->
    <div v-if="status.open" class="modal-mask" @click.self="closeStatus">
      <div class="modal status-modal">
        <template v-if="status.data">
          <div class="status-head">
            <img
              v-if="status.data.character.avatar_url"
              class="avatar big img"
              :src="avatarSrc(status.data.character.avatar_url)"
              :alt="status.data.character.name"
            />
            <div v-else class="avatar big">{{ status.data.character.avatar }}</div>
            <div>
              <h3>
                {{ status.data.character.name }}
                <span v-if="status.data.character.is_user_persona" class="tag user">我</span>
                <span v-else-if="status.data.character.source === 'user'" class="tag custom">自定义</span>
              </h3>
              <p class="sub">
                {{ status.data.character.role || '—' }}
                <template v-if="status.data.character.shelf_title"> · 书架「{{ status.data.character.shelf_title }}」</template>
                <template v-if="status.data.character.book_title"> · 《{{ status.data.character.book_title }}》</template>
              </p>
            </div>
          </div>

          <section class="status-block">
            <h4>此刻状态</h4>
            <div class="status-mood">
              <span class="mood-badge">{{ status.data.status.mood }}</span>
              <span class="mood-text">{{ status.data.status.mood_text }}</span>
            </div>
            <p class="status-line">
              <strong>对你的态度</strong><span>{{ status.data.status.attitude_text }}</span>
            </p>
            <p class="status-line">
              <strong>在场</strong><span>{{ status.data.status.presence }}</span>
            </p>
            <p class="status-line">
              <strong>互动</strong>
              <span>
                {{ status.data.status.turns }} 轮对话
                <template v-if="status.data.status.last_active">，最近一次 {{ formatTime(status.data.status.last_active) }}</template>
              </span>
            </p>
            <p v-if="status.data.member" class="status-line">
              <strong>状态</strong>
              <span>
                {{ status.data.member.status === 'offline' ? '离线' : '在线' }}
                <template v-if="status.data.member.state === 'cooldown'">（刚说过话）</template>
                · {{ status.data.status.mood_text || status.data.status.mood }}
              </span>
            </p>
            <p class="status-line">
              <strong>记忆</strong>
              <span>
                {{ status.data.status.memory.total }} 段原文记忆
                <template v-if="status.data.status.memory.total > 1">（来自 {{ Object.keys(status.data.status.memory.by_source).length }} 本书）</template>
              </span>
            </p>
          <p v-if="status.data.status.memory.summary" class="status-line">
              <strong>记忆摘要</strong><span>{{ status.data.status.memory.summary }}</span>
            </p>
          </section>

          <section class="status-block">
            <h4>基本信息</h4>
            <p class="status-line"><strong>身份</strong><span>{{ status.data.character.role || '—' }}</span></p>
            <p v-if="status.data.character.aliases.length" class="status-line">
              <strong>别名</strong><span>{{ status.data.character.aliases.join('、') }}</span>
            </p>
            <p class="status-line">
              <strong>性格</strong><span>{{ status.data.character.personality.join('、') || '—' }}</span>
            </p>
            <p class="status-line">
              <strong>说话风格</strong><span>{{ status.data.character.speech_style || '—' }}</span>
            </p>
            <p v-if="status.data.character.background" class="status-line">
              <strong>背景</strong><span>{{ status.data.character.background }}</span>
            </p>
            <p v-if="status.data.character.relationships.length" class="status-line">
              <strong>关系</strong>
              <span>
                <template v-for="(rel, index) in status.data.character.relationships" :key="index">
                  {{ rel.with }}（{{ rel.relation }}）<template v-if="index < status.data.character.relationships.length - 1">、</template>
                </template>
              </span>
            </p>
            <p class="status-line">
              <strong>来源</strong>
              <span>
                {{ status.data.character.source === 'user' ? '自定义角色' : '小说抽取' }}
                · 原文出场 {{ status.data.character.appearances }} 次
              </span>
            </p>
          </section>
        </template>

        <p v-else-if="status.loading" class="sub">正在读取状态…</p>
        <p v-else-if="status.error" class="error-text">{{ status.error }}</p>

        <section v-if="status.data?.status?.merged_children?.length" class="status-block">
            <h4>已并入的角色</h4>
            <p
              v-for="child in status.data?.status?.merged_children || []"
              :key="child.id"
              class="status-line"
            >
              <strong>{{ child.name }}</strong>
              <button class="ghost small" @click="unmergeChild(child)">撤销合并</button>
            </p>
          </section>

        <div class="modal-actions">
          <button class="ghost" @click="closeStatus">关闭</button>
          <template v-if="status.data">
            <button class="ghost" @click="openMergePicker">合并到…</button>
            <button class="ghost" @click="toggleMemberStatus">
              {{ status.data.member?.status === 'offline' ? '上线' : '离线' }}
            </button>
            <button class="ghost" @click="editFromStatus()">编辑</button>
            <button class="ghost danger" @click="removeCharacter(status.data.character)">删除</button>
            <button class="primary" @click="chatFromStatus()">
              单独聊
            </button>
          </template>
        </div>
      </div>
    </div>
</template>

<script setup>
import { useApp } from '../../stores/app.js'

// 一次拿全部应用状态与动作；模板里直接写名字
const {
  chatFromStatus,
  editFromStatus,
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
