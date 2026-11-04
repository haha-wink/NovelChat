<template>
    <!-- ============ 群聊（一个书架 = 一个群）============ -->
    <main class="page chat-page">
      <div class="chat-head">
        <button class="ghost" @click="goShelf">← 书架</button>
        <div class="chat-who">
          <div class="avatar">👥</div>
          <div>
            <h3>{{ group.shelfTitle || '群聊' }}</h3>
            <p class="sub">{{ group.members.length }} 位成员 · 点成员看详情，@名字 让他必答</p>
          </div>
        </div>
        <div class="head-actions">
          <button class="ghost small" @click="openGroupInfo">信息</button>
        </div>
      </div>

      <div class="chat-container" ref="groupContainer">
        <div class="chat-messages">
          <div v-if="group.messages.length === 0" class="intro">
            <div class="avatar big">👥</div>
            <h3>《{{ group.shelfTitle }}》的群聊</h3>
            <p>
              群里的人不会每句都接话——他们按自己的性格和心情决定要不要开口。
              想让谁说，就 @ 他；想让大家响应，就 @全体成员。
            </p>
          </div>

          <div
            v-for="(message, index) in group.messages"
            :key="index"
            class="message-wrapper"
            :class="message.kind === 'user' ? 'user' : 'assistant'"
          >
            <template v-if="message.kind === 'system'">
              <div class="system-message">{{ message.content }}</div>
            </template>
            <template v-else>
              <template v-if="message.kind === 'character'">
                <img
                  v-if="message.avatar_url"
                  class="avatar img"
                  :src="avatarSrc(message.avatar_url)"
                  :alt="message.name"
                />
                <div v-else class="avatar">{{ message.avatar }}</div>
              </template>
              <div class="message-content">
                <p v-if="message.kind === 'character'" class="message-speaker">
                  {{ message.name }}
                  <span v-if="message.typing" class="typing-dot">正在输入…</span>
                </p>
                <div class="message-bubble">
                  <div v-if="message.content" v-html="renderMarkdown(message.content)"></div>
                  <div v-else class="typing"><span></span><span></span><span></span></div>
                </div>
                <img
                  v-if="message.sticker && !message.stickerFailed"
                  class="sticker"
                  :src="message.sticker.url"
                  :alt="message.sticker.emoji"
                  @error="message.stickerFailed = true"
                />
                <span v-else-if="message.sticker" class="sticker emoji-only">
                  {{ message.sticker.emoji }}
                </span>
                <p v-if="message.mentions && message.mentions.length" class="message-mentions">
                  @ {{ groupMentionText(message.mentions) }}
                </p>
              </div>
              <div v-if="message.kind === 'user'" class="avatar me">🧑</div>
            </template>
          </div>
        </div>
      </div>

      <div class="chat-input-wrapper">
        <div class="chat-input-box">
          <textarea
            v-model="group.input"
            @keydown="handleGroupKeydown"
            :disabled="group.loading"
            rows="1"
            placeholder="对群里说点什么…（@名字 让他必答，@全体成员 叫醒所有人）"
          ></textarea>
          <button
            class="send-btn"
            :disabled="!group.input.trim() || group.loading"
            @click="sendGroupMessage"
          >
            发送
          </button>
        </div>
        <p class="footer-text">
          群里每位成员都是独立的角色 Agent，有各自的情绪与记忆；不想让谁说话，点上面的成员卡片。
        </p>
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
