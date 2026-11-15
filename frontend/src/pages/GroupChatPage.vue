<template>
    <!-- ============ 群聊（一个书架 = 一个群）============ -->
    <main class="page chat-page group-layout">
      <!-- 左侧：对话列表（一个书架可以有多个对话） -->
      <aside class="conv-sidebar" :style="sidebarStyle">
        <button class="conv-new" :disabled="group.loading" @click="newConversation">
          ＋ 新建对话
        </button>
        <p v-if="group.conversationsLoading" class="conv-hint">正在加载…</p>
        <p v-else-if="!group.conversations.length" class="conv-hint">还没有对话</p>
        <ul v-else class="conv-list">
          <li
            v-for="conversation in group.conversations"
            :key="conversation.id"
            class="conv-item"
            :class="{ active: conversation.id === group.conversationId }"
            @click="switchConversation(conversation.id)"
          >
            <div class="conv-title">{{ conversation.title || '新对话' }}</div>
            <div class="conv-meta">
              {{ conversation.turn_count || 0 }} 条
              <button
                class="conv-del"
                title="删除这个对话"
                @click.stop="deleteConversation(conversation.id)"
              >
                ×
              </button>
            </div>
          </li>
        </ul>
      </aside>

      <!-- 可用鼠标拖动的分界线：悬停时变宽一点给你"能拖"的反馈，按住拖改宽度 -->
      <div
        class="conv-resizer"
        :class="{ active: group.sidebarHover || group.sidebarDragging }"
        title="拖动调整宽度，双击恢复"
        @mouseenter="group.sidebarHover = true"
        @mouseleave="group.sidebarHover = false"
        @mousedown="startSidebarResize"
        @dblclick="resetSidebarWidth"
      >
        <span class="conv-resizer-grip"></span>
      </div>

      <div class="group-main">
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
            v-for="(message, index) in visibleGroupMessages"
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
                  <div v-if="message.images && message.images.length" class="sent-images">
                    <a
                      v-for="(image, imageIndex) in message.images"
                      :key="imageIndex"
                      :href="image"
                      target="_blank"
                      rel="noreferrer"
                    >
                      <img
                        class="sent-image"
                        :src="image"
                        alt="已发送的图片"
                        @error="$event.target.style.display = 'none'"
                      />
                    </a>
                  </div>
                  <div v-if="message.content" v-html="renderMarkdown(message.content)"></div>
                  <div v-else class="typing"><span></span><span></span><span></span></div>
                </div>
                <span v-if="message.at && !message.typing" class="message-time">
                  {{ formatTime(message.at) }}
                </span>
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
        <div v-if="pendingImages.length > 0" class="pending-images">
          <div v-for="(image, index) in pendingImages" :key="index" class="preview-item">
            <img :src="image.base64" alt="preview" />
            <button class="remove-btn" @click="pendingImages.splice(index, 1)">×</button>
          </div>
        </div>

        <div class="chat-input-box">
          <label for="group-file-upload" class="upload-btn" title="上传图片（可选）">
            📎
            <input
              id="group-file-upload"
              type="file"
              accept="image/png, image/jpeg, image/jpg"
              hidden
              @change="handleFileSelect"
            />
          </label>
          <textarea
            v-model="group.input"
            @keydown="handleGroupKeydown"
            :disabled="group.loading"
            rows="1"
            placeholder="对群里说点什么…（@名字 让他必答，@全体成员 叫醒所有人）"
          ></textarea>
          <button
            class="send-btn"
            :disabled="(!group.input.trim() && pendingImages.length === 0) || group.loading"
            @click="sendGroupMessage"
          >
            发送
          </button>
        </div>
        <p class="footer-text">
          群里每位成员都是独立的角色 Agent，有各自的情绪与记忆；不想让谁说话，点上面的成员卡片。
        </p>
      </div>
      </div>
    </main>
</template>

<script setup>
import { computed, onMounted } from 'vue'

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
  visibleGroupMessages,
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
  newConversation,
  switchConversation,
  deleteConversation,
  restoreSidebarWidth,
  startSidebarResize,
  resetSidebarWidth,
  sidebarWidthPx,
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

//: 宽度只由「拖动」决定。
//: 曾经试过"悬停自动加宽"，但加宽会把分界线推到鼠标左边 ->
//: mouseleave -> 缩回 -> 分界线又回到鼠标下 -> mouseenter -> 无限震荡。
//: 悬停只做高亮反馈（纯视觉，不动尺寸），改宽交给拖动。
const sidebarStyle = computed(() => {
  const px = sidebarWidthPx()
  if (!px) return {}
  return {
    width: px,
    transition: group.sidebarDragging ? 'none' : 'width 0.16s ease'
  }
})

onMounted(() => {
  restoreSidebarWidth()
})
</script>
