<template>
    <!-- ============ 聊天 ============ -->
    <main class="page chat-page">
      <div class="chat-head">
        <button class="ghost" @click="backToGroup">← 群聊</button>
        <div class="chat-who clickable" @click="openStatus(activeCharacter)" title="查看角色详情与状态">
          <img
            v-if="activeCharacter?.avatar_url"
            class="avatar img"
            :src="avatarSrc(activeCharacter.avatar_url)"
            :alt="activeCharacter?.name"
          />
          <div v-else class="avatar">{{ activeCharacter?.avatar }}</div>
          <div>
            <h3>{{ activeCharacter?.name }}</h3>
            <p class="sub">{{ activeCharacter?.role || '—' }} · 《{{ activeBook?.title }}》</p>
          </div>
        </div>
        <button
          class="ghost"
          :disabled="isLoading"
          title="对话先蒸馏成长期记忆再清空"
          @click="clearCharacterHistory(activeCharacter.id, 'distill')"
        >
          清空对话
        </button>
        <button
          class="ghost danger"
          :disabled="isLoading"
          title="对话和记忆一起删除"
          @click="clearCharacterHistory(activeCharacter.id, 'purge')"
        >
          清空对话和记忆
        </button>
        <button class="ghost" :disabled="isLoading" @click="newChat">新对话</button>
      </div>

      <div class="chat-container" ref="chatContainer">
        <div class="chat-messages">
          <div v-if="currentMessages.length === 0" class="intro">
            <img
              v-if="activeCharacter?.avatar_url"
              class="avatar big img"
              :src="avatarSrc(activeCharacter.avatar_url)"
              :alt="activeCharacter?.name"
            />
            <div v-else class="avatar big">{{ activeCharacter?.avatar }}</div>
            <h3>{{ activeCharacter?.name }}</h3>
            <p>{{ activeCharacter?.background || '（这个角色在原文里没有留下太多介绍）' }}</p>
            <div class="chips center">
              <span v-for="(trait, index) in (activeCharacter?.personality || []).slice(0, 5)" :key="index" class="chip">
                {{ trait }}
              </span>
            </div>
            <p class="hint">说点什么，开始你们的故事。</p>
          </div>

          <div
            v-for="(message, index) in currentMessages"
            :key="index"
            :class="['message-wrapper', message.role]"
          >
            <template v-if="message.role !== 'system'">
              <img
                v-if="message.role === 'assistant' && activeCharacter?.avatar_url"
                class="avatar img"
                :src="avatarSrc(activeCharacter.avatar_url)"
                :alt="activeCharacter?.name"
              />
              <div v-else-if="message.role === 'assistant'" class="avatar">{{ activeCharacter?.avatar }}</div>
              <div class="message-content">
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
                  <div v-else-if="message.isImage" class="image-placeholder">
                    🖼️ [已上传截图: {{ message.fileName }}]
                  </div>
                  <div v-else v-html="renderMarkdown(message.content)"></div>
                </div>
                <span v-if="message.at" class="message-time">{{ formatTime(message.at) }}</span>
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
              </div>
              <div v-if="message.role === 'user'" class="avatar me">🧑</div>
            </template>
            <div v-else class="system-message">{{ message.content }}</div>
          </div>

          <div v-if="isLoading && !currentMessages[currentMessages.length - 1]?.content" class="message-wrapper assistant">
            <img
              v-if="activeCharacter?.avatar_url"
              class="avatar img"
              :src="avatarSrc(activeCharacter.avatar_url)"
              :alt="activeCharacter?.name"
            />
            <div v-else class="avatar">{{ activeCharacter?.avatar }}</div>
            <div class="message-content">
              <div class="message-bubble typing"><span></span><span></span><span></span></div>
            </div>
          </div>
        </div>
      </div>

      <div class="chat-input-wrapper">
        <!-- 上一轮生成中断（网络抖动/切页面）：这里可以接着生成，不用重新提问 -->
        <div v-if="currentConversation?.canResume" class="resume-banner">
          <span>上一轮回复中断了</span>
          <button class="resume-btn" :disabled="isLoading" @click="resumeChat">继续</button>
        </div>

        <div v-if="pendingImages.length > 0" class="pending-images">
          <div v-for="(image, index) in pendingImages" :key="index" class="preview-item">
            <img :src="image.base64" alt="preview" />
            <button class="remove-btn" @click="pendingImages.splice(index, 1)">×</button>
          </div>
        </div>

        <div class="chat-input-box">
          <label for="file-upload" class="upload-btn" title="上传图片（可选）">
            📎
            <input
              id="file-upload"
              type="file"
              accept="image/png, image/jpeg, image/jpg"
              hidden
              @change="handleFileSelect"
            />
          </label>
          <textarea
            ref="textareaRef"
            v-model="chatInput"
            @keydown="handleKeydown"
            :disabled="isLoading"
            rows="1"
            :placeholder="`对${activeCharacter?.name || '角色'}说点什么…（Enter 发送，Shift+Enter 换行）`"
          ></textarea>
          <button
            class="send-btn"
            :disabled="(!chatInput.trim() && pendingImages.length === 0) || isLoading"
            @click="sendMessage"
          >
            发送
          </button>
        </div>
        <p class="footer-text">角色由 AI 依据小说设定扮演，内容仅供娱乐。</p>
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
  resumeChat,
  currentConversation,
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
  clearCharacterHistory,
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
