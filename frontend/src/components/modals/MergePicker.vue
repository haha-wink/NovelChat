<template>
    <!-- ============ 手动合并弹窗 ============ -->
    <div v-if="merge.open" class="modal-mask" @click.self="merge.open = false">
      <div class="modal merge-modal">
        <h3>把「{{ status.data?.character?.name }}」合并到谁？</h3>
        <p class="sub">
          合并后：记忆相加并重新生成摘要，对方会把这个名字记为别名，之后自动识别也能认出。
        </p>
        <div class="merge-list">
          <button
            v-for="member in mergeCandidates()"
            :key="member.character_id"
            class="merge-item"
            :class="{ active: merge.targetId === member.character_id }"
            @click="merge.targetId = member.character_id"
          >
            <img
              v-if="member.avatar_url"
              class="avatar small img"
              :src="avatarSrc(member.avatar_url)"
              :alt="member.name"
            />
            <span v-else class="avatar small">{{ member.avatar }}</span>
            <span class="merge-name">{{ member.name }}</span>
            <span class="sub">{{ member.role || '' }}</span>
          </button>
          <p v-if="!mergeCandidates().length" class="sub">群里没有别的角色可以合并</p>
        </div>
        <p v-if="merge.error" class="error">{{ merge.error }}</p>
        <div class="modal-actions">
          <button class="ghost" @click="merge.open = false">取消</button>
          <button
            class="primary"
            :disabled="!merge.targetId || merge.busy"
            @click="confirmMerge"
          >
            {{ merge.busy ? '合并中…' : '合并' }}
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
