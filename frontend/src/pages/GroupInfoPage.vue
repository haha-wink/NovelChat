<template>
    <!-- ============ 群聊信息（独立界面，和「书架」一样是跳转）============ -->
    <main class="page">
      <div class="chat-head">
        <button class="ghost" @click="closeGroupInfo">← 群聊</button>
        <div class="chat-who">
          <div class="avatar">⚙️</div>
          <div>
            <h3>群聊信息（{{ group.members.length }}）</h3>
            <p class="sub">{{ group.shelfTitle || '群聊' }} · 成员与设置</p>
          </div>
        </div>
        <div class="head-actions">
          <button class="ghost small" :disabled="group.loading" @click="loadGroupMembers">刷新</button>
        </div>
      </div>

      <section class="info-panel">
        <label class="info-field">
          <span>群聊名称</span>
          <input v-model="groupInfo.title" maxlength="30" placeholder="给这个群起个名字" />
          <button class="primary small" :disabled="groupInfo.saving" @click="saveGroupTitle">
            {{ groupInfo.saving ? '保存中…' : '保存' }}
          </button>
        </label>
        <p v-if="groupInfo.error" class="error">{{ groupInfo.error }}</p>

        <h4 class="info-sub">成员信息</h4>
        <div class="member-grid">
          <button
            v-for="member in visibleMembers"
            :key="member.character_id"
            class="member-cell"
            :class="{ offline: member.status === 'offline' }"
            :title="member.status === 'offline' ? '离线' : '在线'"
            @click="openMemberStatus(member)"
          >
            <img
              v-if="member.avatar_url"
              class="avatar img"
              :src="avatarSrc(member.avatar_url)"
              :alt="member.name"
            />
            <span v-else class="avatar">{{ member.avatar }}</span>
            <span class="member-cell-name">{{ member.name }}</span>
          </button>

          <button v-if="hasMoreMembers" class="member-cell more" @click="groupInfo.expanded = true">
            <span class="avatar more-icon">⋯</span>
            <span class="member-cell-name">更多</span>
          </button>
        </div>
        <p v-if="!group.members.length" class="empty-mini">这个书架还没有角色</p>

        <div class="info-actions">
          <button class="ghost" :disabled="!activeBook" @click="openEditor(null)">
            ＋ 添加角色
          </button>
          <button class="ghost" :disabled="verifying || !activeBook" @click="verifyBook">
            {{ verifying ? '审核中…' : '🛡 AI 审核' }}
          </button>
          <button class="ghost danger" @click="clearGroupHistory">清空对话</button>
        </div>
      </section>

      <!-- 待确认的合并：名字不同、但关系网/性格很像的角色 -->
      <section class="info-panel">
        <h4 class="info-sub">
          待确认的合并（{{ mergePanel.pairs.length }}）
          <span class="merge-hint">已记住 {{ mergePanel.learned }} 条合并经验</span>
        </h4>

        <p v-if="mergePanel.loading" class="empty-mini">正在找可疑的重复角色…</p>
        <p v-else-if="mergePanel.error" class="error">{{ mergePanel.error }}</p>
        <p v-else-if="!mergePanel.pairs.length" class="empty-mini">
          没有需要确认的候选。同名角色会自动合并；相似度高于 0.7 的也会直接合并，
          这里只出现"名字不同、有点像但不那么确定"的情况。
        </p>

        <div
          v-for="pair in mergePanel.pairs"
          :key="pair.left.id + pair.right.id"
          class="merge-row"
        >
          <div class="merge-names">
            <strong>{{ pair.left.name }}</strong>
            <span class="merge-vs">↔</span>
            <strong>{{ pair.right.name }}</strong>
          </div>
          <div class="merge-meta">
            <span>相似度 {{ pair.score }}</span>
            <span v-if="pair.common.length">共同关联：{{ pair.common.join('、') }}</span>
          </div>
          <div class="merge-actions">
            <button
              class="primary small"
              :disabled="mergePanel.busy === pair.left.id"
              @click="resolveMerge(pair, true)"
            >
              是同一人
            </button>
            <button
              class="ghost small"
              :disabled="mergePanel.busy === pair.left.id"
              @click="resolveMerge(pair, false)"
            >
              不是
            </button>
          </div>
        </div>
      </section>
    </main>
</template>

<script setup>
import { useApp } from '../stores/app.js'

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
