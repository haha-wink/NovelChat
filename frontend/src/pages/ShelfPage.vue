<template>
    <!-- ============ 书架（书库 → 书架 → 书）============ -->
    <main class="page">
      <div class="shelf-toolbar">
        <label class="upload-card compact" :class="{ busy: uploading }">
          <input
            type="file"
            accept=".txt,.md,.markdown,.pdf"
            hidden
            :disabled="uploading"
            @change="handleUpload"
          />
          <span class="upload-icon">{{ uploading ? '⏳' : '＋' }}</span>
          <span class="upload-title">
            {{ uploading ? '正在上传并解析…' : '上传小说（txt / md / pdf）' }}
          </span>
        </label>

        <label v-if="shelves.length" class="shelf-picker">
          <span>上传到</span>
          <select v-model="uploadShelfId">
            <option v-for="shelf in shelves" :key="shelf.id" :value="shelf.id">
              {{ shelf.title }}
            </option>
          </select>
        </label>

        <button class="primary" @click="createShelf">＋ 新建书架</button>
      </div>

      <p v-if="!shelves.length && !loadingBooks" class="empty">
        还没有书架。先点「新建书架」，再往里放小说。
      </p>

      <div class="shelf-list">
        <article v-for="shelf in shelves" :key="shelf.id" class="shelf-card">
          <header class="shelf-head">
            <div class="shelf-title">
              <h3>{{ shelf.title }}</h3>
              <p class="meta">
                {{ shelf.book_count }} 本书 · {{ shelf.character_count }} 个角色
              </p>
            </div>
            <div class="shelf-actions">
              <button class="primary" @click="openShelfChat(shelf)">群聊</button>
              <button class="ghost" @click="renameShelf(shelf)">改名</button>
              <button class="ghost danger" @click="removeShelf(shelf)">删除</button>
            </div>
          </header>

          <div class="shelf-books">
            <div
              v-for="book in booksOfShelf(shelf.id)"
              :key="book.id"
              class="shelf-book"
              :class="{ ready: book.status === 'ready' }"
              @click="book.status === 'ready' && openShelfChat(shelf)"
            >
              <span class="book-name">{{ book.title }}</span>
              <span class="book-meta">
                {{ formatCount(book.char_count) }} 字 · {{ book.character_count }} 角色
              </span>
              <div
                v-if="book.status === 'extracting'"
                class="progress inline"
                :style="{ width: Math.max(3, book.progress) + '%' }"
              ></div>
              <span class="status" :class="book.status">{{ statusText(book) }}</span>
              <button class="ghost danger tiny" @click.stop="removeBook(book)">删除</button>
            </div>
            <p v-if="!booksOfShelf(shelf.id).length" class="empty-mini">这个书架还没有书</p>
          </div>
        </article>
      </div>
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
