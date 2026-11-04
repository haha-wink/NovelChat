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

    <!-- ============ 书架（书库 → 书架 → 书）============ -->
    <main v-if="view === 'shelf'" class="page">
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

    <!-- ============ 角色列表 ============ -->
    <!-- ============ 群聊（一个书架 = 一个群）============ -->
    <main v-else-if="view === 'group'" class="page chat-page">
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

    <!-- ============ 群聊信息（独立界面，和「书架」一样是跳转）============ -->
    <main v-else-if="view === 'groupInfo'" class="page">
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

    <!-- ============ 聊天 ============ -->
    <main v-else class="page chat-page">
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
                  <div v-if="message.isImage" class="image-placeholder">
                    🖼️ [已上传截图: {{ message.fileName }}]
                  </div>
                  <div v-else v-html="renderMarkdown(message.content)"></div>
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

        <section v-if="status.data.status.merged_children.length" class="status-block">
            <h4>已并入的角色</h4>
            <p
              v-for="child in status.data.status.merged_children"
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
            <button class="ghost" @click="closeStatus(); openEditor(status.data.character)">编辑</button>
            <button class="ghost danger" @click="removeCharacter(status.data.character)">删除</button>
            <button class="primary" @click="closeStatus(); openChat(status.data.character)">
              单独聊
            </button>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from './api/client.js'
import { readSSEStream } from './utils/sse.js'
import {
  avatarSrc,
  formatCount,
  formatTime,
  groupMentionText,
  memberStateText,
  statusText
} from './utils/format.js'
import { renderMarkdown } from './utils/markdown.js'

const MAX_IMAGES = 4
const MAX_IMAGE_BYTES = 8 * 1024 * 1024
const NAME_KEY = 'novel:reader-name'
//: 记住上次进的群（书架），下次打开直接回到那里
const LAST_SHELF_KEY = 'novel:last-shelf'

// ---------------- 基础状态 ----------------
const view = ref('shelf') // shelf | characters | chat
const health = ref(null)
const error = ref('')
const myName = ref(localStorage.getItem(NAME_KEY) || '读者')

const books = ref([])
const loadingBooks = ref(false)
const uploading = ref(false)

const activeBook = ref(null)
const characters = ref([]) //: 保留空数组以兼容旧引用，实际角色走群成员
const activeCharacter = ref(null)

const chatInput = ref('')
const isLoading = ref(false)
const chatContainer = ref(null)
const textareaRef = ref(null)
const pendingImages = ref([])

//: characterId -> { messages: [], token: string|null }
const conversations = reactive({})

const editor = reactive({
  open: false,
  mode: 'create',
  characterId: '',
  form: {
    name: '',
    role: '',
    aliasesText: '',
    personalityText: '',
    speech_style: '',
    background: '',
    avatar: '',
    avatar_url: '',
    is_user_persona: false
  },
  candidates: [],
  crawling: false,
  avatarError: '',
  saving: false,
  error: ''
})

//: 角色详情 / 状态面板（点角色卡片或聊天头部打开）
const status = reactive({
  open: false,
  loading: false,
  error: '',
  data: null
})

//: 群聊：一个书架 = 一个群
const group = reactive({
  shelfId: '',
  shelfTitle: '',
  members: [],
  messages: [],
  input: '',
  token: '',
  loading: false,
  error: ''
})
const groupContainer = ref(null)

//: 书架列表（书库 → 书架 → 书）
const shelves = ref([])
//: 上传目标书架
const uploadShelfId = ref('')

//: 群聊信息面板
const groupInfo = reactive({
  expanded: false,
  title: '',
  saving: false,
  error: ''
})

//: 待确认的合并候选（名字不同但可能是同一个人）
const mergePanel = reactive({
  loading: false,
  pairs: [],
  learned: 0,
  busy: '',
  error: ''
})

//: 成员网格默认 5 列 × 3 行，超出才出现「更多」
const MEMBER_COLS = 5
const MEMBER_ROWS = 3

//: 手动合并：把当前详情里的角色并入另一个角色
const merge = reactive({
  open: false,
  targetId: '',
  busy: false,
  error: ''
})

//: 角色名单审核中
const verifying = ref(false)

const currentMessages = computed(() => {
  const id = activeCharacter.value?.id
  if (!id) return []
  return conversations[id]?.messages || []
})

// ---------------- 请求封装 ----------------
async function loadHealth() {
  try {
    health.value = await api('/api/health')
  } catch (failure) {
    health.value = { api_key_configured: null }
    error.value = `连不上后端：${failure.message}`
  }
}

async function loadBooks() {
  loadingBooks.value = true
  try {
    const [shelfList, bookList] = await Promise.all([api('/api/shelves'), api('/api/books')])
    shelves.value = shelfList
    books.value = bookList
    // 上传默认落到第一个书架
    if (!uploadShelfId.value && shelfList.length) uploadShelfId.value = shelfList[0].id
    if (uploadShelfId.value && !shelfList.some((item) => item.id === uploadShelfId.value)) {
      uploadShelfId.value = shelfList[0]?.id || ''
    }
  } catch (failure) {
    error.value = failure.message
  } finally {
    loadingBooks.value = false
    schedulePoll()
  }
}

//: 某个书架下的书
function booksOfShelf(shelfId) {
  return books.value.filter((book) => book.shelf_id === shelfId)
}

// ---------------- 上传与抽取进度 ----------------
let pollTimer = null

function schedulePoll() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
  if (view.value !== 'shelf' && view.value !== 'group' && view.value !== 'groupInfo') return
  if (!books.value.some((book) => book.status === 'extracting')) return
  pollTimer = setInterval(async () => {
    try {
      books.value = await api('/api/books')
      // 抽取完成后刷新群成员（角色是群成员，不再有独立列表）
      if (view.value === 'group' && activeBook.value) {
        const fresh = books.value.find((book) => book.id === activeBook.value.id)
        if (fresh && fresh.status === 'ready') await loadGroupMembers()
      }
      if (!books.value.some((book) => book.status === 'extracting')) schedulePoll()
    } catch (failure) {
      schedulePoll()
    }
  }, 1500)
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return

  error.value = ''
  uploading.value = true
  try {
    const form = new FormData()
    form.append('file', file)
    form.append('title', file.name.replace(/\.[^.]+$/, ''))
    if (uploadShelfId.value) form.append('shelf_id', uploadShelfId.value)
    const response = await fetch('/api/books', { method: 'POST', body: form })
    if (!response.ok) {
      let detail = `上传失败（HTTP ${response.status}）`
      try {
        const data = await response.json()
        if (data?.detail) detail = data.detail
      } catch (parseError) {
        /* ignore */
      }
      throw new Error(detail)
    }
    const book = await response.json()
    books.value = [book, ...books.value.filter((item) => item.id !== book.id)]
    schedulePoll()
  } catch (failure) {
    error.value = failure.message
  } finally {
    uploading.value = false
  }
}

async function retryBook(book) {
  error.value = ''
  try {
    await api(`/api/books/${book.id}/retry`, { method: 'POST' })
    await loadBooks()
  } catch (failure) {
    error.value = failure.message
  }
}

async function removeBook(book) {
  if (!confirm(`删除《${book.title}》？角色和相关记录都会一起删掉。`)) return
  try {
    await api(`/api/books/${book.id}`, { method: 'DELETE' })
    books.value = books.value.filter((item) => item.id !== book.id)
    if (activeBook.value?.id === book.id) goShelf()
  } catch (failure) {
    error.value = failure.message
  }
}

// ---------------- 书架（新建 / 改名 / 删除 / 进群）----------------
async function createShelf() {
  const libraryId = shelves.value[0]?.library_id
  if (!libraryId) {
    error.value = '没有可用的书库，无法新建书架'
    return
  }
  const title = (prompt('新书架名称', '新书架') || '').trim()
  if (!title) return
  try {
    const created = await api('/api/shelves', {
      method: 'POST',
      body: { library_id: libraryId, title }
    })
    await loadBooks()
    uploadShelfId.value = created.id
  } catch (failure) {
    error.value = failure.message
  }
}

async function renameShelf(shelf) {
  const title = (prompt('书架名称（群聊名称同步修改）', shelf.title) || '').trim()
  if (!title || title === shelf.title) return
  try {
    await api(`/api/shelves/${shelf.id}`, { method: 'PATCH', body: { title } })
    if (group.shelfId === shelf.id) group.shelfTitle = title
    await loadBooks()
  } catch (failure) {
    error.value = failure.message
  }
}

async function removeShelf(shelf) {
  const count = booksOfShelf(shelf.id).length
  const message = count
    ? `删除书架「${shelf.title}」会同时删除里面的 ${count} 本书、它们的角色和聊天记录，不可恢复。确定吗？`
    : `删除书架「${shelf.title}」？`
  if (!confirm(message)) return
  try {
    await api(`/api/shelves/${shelf.id}`, { method: 'DELETE' })
    if (group.shelfId === shelf.id) goShelf()
    await loadBooks()
  } catch (failure) {
    error.value = failure.message
  }
}

//: 从书架卡片直接进群（= 原来「选择角色」的功能）
async function openShelfChat(shelf) {
  const shelfBooks = booksOfShelf(shelf.id)
  const book = shelfBooks.find((item) => item.status === 'ready') || shelfBooks[0] || null
  await enterGroup(shelf.id, shelf.title, book)
}

async function enterGroup(shelfId, title, book = null) {
  group.shelfId = shelfId
  group.shelfTitle = title || ''
  group.messages = []
  group.error = ''
  activeBook.value = book
  view.value = 'group'
  try {
    localStorage.setItem(LAST_SHELF_KEY, shelfId)
  } catch (storageError) {
    // 隐私模式下 localStorage 可能不可用，不影响主流程
  }
  await loadGroupMembers()
  await loadGroupHistory()
}

// ---------------- 作品 / 角色 ----------------
async function openBook(book) {
  const shelf = shelves.value.find((item) => item.id === book.shelf_id)
  await enterGroup(book.shelf_id, shelf?.title || book.title, book)
}

function goShelf() {
  view.value = 'shelf'
  activeBook.value = null
  activeCharacter.value = null
  group.messages = []
  group.members = []
  groupInfo.expanded = false
  loadBooks()
}

function backToGroup() {
  view.value = 'group'
  activeCharacter.value = null
  loadGroupMembers()
}

async function openChat(character) {
  activeCharacter.value = character
  view.value = 'chat'
  const conversation =
    conversations[character.id] || (conversations[character.id] = { messages: [], token: null })
  // 每次进来都要能看到历史（后端 chat_turns 就是历史源）
  if (!conversation.messages.length) await loadChatHistory(character)
  nextTick(scrollToBottom)
}

//: 单聊历史
async function loadChatHistory(character) {
  try {
    const token = await ensureToken(character.id)
    const query = `?token=${encodeURIComponent(token)}&limit=80`
    const data = await api(`/api/characters/${character.id}/history${query}`)
    const conversation =
      conversations[character.id] || (conversations[character.id] = { messages: [], token: null })
    conversation.messages = (data.turns || []).map((turn) => ({
      role: turn.role === 'user' ? 'user' : 'assistant',
      content: turn.content || ''
    }))
  } catch (failure) {
    // 历史拉不到不该挡着聊天，静默忽略
  }
}

async function newChat() {
  const character = activeCharacter.value
  if (!character) return
  if (
    !confirm(
      [
        '开一段新对话？',
        '',
        '· 之前的聊天记录会一起清掉',
        `· ${character.name} 已经记住的事不受影响`,
        '· 不可恢复'
      ].join('\n')
    )
  ) {
    return
  }

  // 先清后端流水，否则重进单聊又会把旧记录拉回来
  try {
    const oldToken = localStorage.getItem(tokenStorageKey(character.id))
    if (oldToken) {
      await api(
        `/api/characters/${character.id}/history?token=${encodeURIComponent(oldToken)}`,
        { method: 'DELETE' }
      )
    }
  } catch (failure) {
    // 清历史失败不该挡住开新对话
  }

  conversations[character.id] = { messages: [], token: null }
  localStorage.removeItem(tokenStorageKey(character.id))
  await ensureToken(character.id)
  scrollToBottom()
}

async function removeCharacter(character) {
  if (!confirm(`删除角色「${character.name}」？`)) return
  try {
    await api(`/api/characters/${character.id}`, { method: 'DELETE' })
    closeStatus()
    await loadGroupMembers()
  } catch (failure) {
    error.value = failure.message
  }
}

// ---------------- 角色编辑 ----------------
function openEditor(character) {
  editor.open = true
  editor.error = ''
  editor.avatarError = ''
  editor.crawling = false
  editor.candidates = (character?.avatar_candidates || []).slice()
  editor.mode = character ? 'edit' : 'create'
  editor.characterId = character?.id || ''
  editor.form = {
    name: character?.name || '',
    role: character?.role || '',
    aliasesText: (character?.aliases || []).join('、'),
    personalityText: (character?.personality || []).join('、'),
    speech_style: character?.speech_style || '',
    background: character?.background || '',
    avatar: character?.avatar || '',
    avatar_url: character?.avatar_url || '',
    is_user_persona: Boolean(character?.is_user_persona)
  }
}

function closeEditor() {
  editor.open = false
  editor.saving = false
}

// ---------------- 角色详情 / 状态 ----------------
function closeStatus() {
  status.open = false
  status.data = null
  status.error = ''
  status.loading = false
}

async function openStatus(character) {
  if (!character?.id) return
  status.open = true
  status.loading = true
  status.error = ''
  status.data = null
  try {
    const token = await ensureToken(character.id)
    const query = token ? `?token=${encodeURIComponent(token)}` : ''
    status.data = await api(`/api/characters/${character.id}/status${query}`)
  } catch (failure) {
    status.error = failure.message
  } finally {
    status.loading = false
  }
}

//: 群成员点开详情（成员对象只有 character_id，详情接口要 id）
function openMemberStatus(member) {
  return openStatus({ id: member?.character_id })
}

//: 手动合并：候选是群里除自己以外的成员
function mergeCandidates() {
  const currentId = status.data?.character?.id
  return group.members.filter((member) => member.character_id !== currentId)
}

function openMergePicker() {
  merge.open = true
  merge.targetId = ''
  merge.error = ''
}

async function confirmMerge() {
  const current = status.data?.character
  if (!current?.id || !merge.targetId) return
  merge.busy = true
  merge.error = ''
  try {
    await api(`/api/characters/${current.id}/merge`, {
      method: 'POST',
      body: { target_id: merge.targetId }
    })
    merge.open = false
    closeStatus()
    await loadGroupMembers()
  } catch (failure) {
    merge.error = failure.message
  } finally {
    merge.busy = false
  }
}

async function unmergeChild(child) {
  if (!confirm(`撤销合并，让「${child.name}」重新独立？`)) return
  try {
    await api(`/api/characters/${child.id}/unmerge`, { method: 'POST' })
    await loadGroupMembers()
    if (status.open && status.data?.character) await openStatus(status.data.character)
  } catch (failure) {
    status.error = failure.message
  }
}

//: 详情面板里切换「在线 / 离线」
async function toggleMemberStatus() {
  const data = status.data
  if (!data?.member || !data?.character?.id) return
  const next = data.member.status === 'offline' ? 'online' : 'offline'
  try {
    await api(`/api/shelves/${data.character.shelf_id}/members/${data.character.id}`, {
      method: 'PATCH',
      body: { status: next }
    })
    data.member.status = next
    await loadGroupMembers()
  } catch (failure) {
    status.error = failure.message
  }
}

//: 头像统一走代理，避免第三方图片热链被拒
async function crawlAvatars() {
  if (!editor.characterId) return
  editor.crawling = true
  editor.avatarError = ''
  try {
    const data = await api(`/api/characters/${editor.characterId}/avatars`, {
      method: 'POST',
      body: { work_title: activeBook.value?.title || '' }
    })
    editor.candidates = data.candidates || []
    if (editor.candidates.length === 0) {
      // 把每个数据源的真实失败原因显示出来，而不是只说"没找到"
      editor.avatarError = data.explain
        ? `没抓到可用头像：${data.explain}`
        : '没找到可用头像，先用 emoji 吧'
    }
  } catch (failure) {
    editor.avatarError =
      failure.status === 404
        ? '后端还没有头像接口 —— 请重启后端（Ctrl+C 后重新运行 dev.ps1）'
        : failure.message
  } finally {
    editor.crawling = false
  }
}

// ---------------- 角色名单 AI 审核 ----------------
async function verifyBook() {
  if (!activeBook.value || verifying.value) return
  verifying.value = true
  error.value = ''
  try {
    const report = await api(`/api/books/${activeBook.value.id}/verify`, { method: 'POST' })
    await loadGroupMembers()
    const kept = report?.report?.kept ?? report?.characters ?? '?'
    const dropped = (report?.report?.dropped || []).length
    const renamed = (report?.report?.renamed || []).length
    alert(
      `AI 审核完成：保留 ${kept} 个角色` +
        (dropped ? `，剔除 ${dropped} 个` : '') +
        (renamed ? `，合并 ${renamed} 组同人` : '') +
        `（共走了 ${report?.report?.steps ?? '?'} 步）`
    )
  } catch (failure) {
    error.value = failure.message
  } finally {
    verifying.value = false
  }
}

function parsePersonality(text) {
  return String(text || '')
    .split(/[,，、;；/|]+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 8)
}

async function saveCharacter() {
  if (!activeBook.value) return
  if (!editor.form.name.trim()) {
    editor.error = '角色名不能为空'
    return
  }
  editor.saving = true
  editor.error = ''
  try {
    const payload = {
      name: editor.form.name.trim(),
      role: editor.form.role.trim(),
      aliases: parsePersonality(editor.form.aliasesText),
      personality: parsePersonality(editor.form.personalityText),
      speech_style: editor.form.speech_style.trim(),
      background: editor.form.background.trim()
    }
    if (editor.mode === 'create') {
      payload.avatar = editor.form.avatar.trim()
      payload.is_user_persona = editor.form.is_user_persona
      await api(`/api/books/${activeBook.value.id}/characters`, {
        method: 'POST',
        body: payload
      })
    } else {
      payload.avatar_url = editor.form.avatar_url || ''
      if (editor.form.avatar.trim()) payload.avatar = editor.form.avatar.trim()
      const updated = await api(`/api/characters/${editor.characterId}`, {
        method: 'PATCH',
        body: payload
      })
      if (activeCharacter.value?.id === updated.id) activeCharacter.value = updated
    }
    closeEditor()
    await loadGroupMembers()
  } catch (failure) {
    editor.error = failure.message
  } finally {
    editor.saving = false
  }
}

// ---------------- 会话 token ----------------
function tokenStorageKey(characterId) {
  return `novel:token:${myName.value}:${characterId}`
}

function onNameChange() {
  const name = (myName.value || '').trim() || '读者'
  myName.value = name
  localStorage.setItem(NAME_KEY, name)
  // 换了身份就是换了人，所有会话都要重开
  Object.keys(conversations).forEach((id) => {
    conversations[id].token = null
    localStorage.removeItem(tokenStorageKey(id))
  })
  // 群聊身份同样重开
  group.token = ''
  group.messages = []
  localStorage.removeItem(`novel:gtoken:${name}`)
}

async function ensureToken(characterId) {
  const conversation = conversations[characterId] || (conversations[characterId] = { messages: [], token: null })
  if (conversation.token) return conversation.token

  const cached = localStorage.getItem(tokenStorageKey(characterId))
  if (cached) {
    conversation.token = cached
    return cached
  }

  const session = await api('/api/session', {
    method: 'POST',
    body: { display_name: myName.value }
  })
  conversation.token = session.token
  localStorage.setItem(tokenStorageKey(characterId), session.token)
  return session.token
}

function refreshToken(characterId) {
  localStorage.removeItem(tokenStorageKey(characterId))
  if (conversations[characterId]) conversations[characterId].token = null
  return ensureToken(characterId)
}

// ---------------- 聊天 ----------------
function handleKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    sendMessage()
  }
}

function handleFileSelect(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return

  if (pendingImages.value.length >= MAX_IMAGES) {
    alert(`最多只能上传 ${MAX_IMAGES} 张图片`)
    return
  }
  if (file.size > MAX_IMAGE_BYTES) {
    alert(`图片超过 ${MAX_IMAGE_BYTES / 1024 / 1024}MB，请压缩后再上传`)
    return
  }

  const reader = new FileReader()
  reader.onload = (loadEvent) => {
    pendingImages.value.push({ base64: loadEvent.target.result, fileName: file.name })
  }
  reader.readAsDataURL(file)
}

async function scrollToBottom() {
  await nextTick()
  if (chatContainer.value) chatContainer.value.scrollTop = chatContainer.value.scrollHeight
}

async function sendMessage() {
  const character = activeCharacter.value
  if (!character) return
  if ((!chatInput.value.trim() && pendingImages.value.length === 0) || isLoading.value) return

  const conversation = conversations[character.id] || (conversations[character.id] = { messages: [], token: null })
  const userText = chatInput.value.trim() || '请看看这张图。'

  const imagesPayload = []
  pendingImages.value.forEach((image) => {
    conversation.messages.push({ role: 'user', content: '[已上传截图]', isImage: true, fileName: image.fileName })
    imagesPayload.push(image.base64)
  })
  conversation.messages.push({ role: 'user', content: userText })

  chatInput.value = ''
  pendingImages.value = []
  scrollToBottom()
  isLoading.value = true

  try {
    let token = await ensureToken(character.id)
    let response = await fetchChat(token, character.id, userText, imagesPayload)

    if (response.status === 401) {
      token = await refreshToken(character.id)
      response = await fetchChat(token, character.id, userText, imagesPayload)
    }

    if (!response.ok) {
      let detail = `请求失败（HTTP ${response.status}）`
      try {
        const data = await response.json()
        if (data?.detail) detail = data.detail
      } catch (parseError) {
        /* ignore */
      }
      throw new Error(detail)
    }

    conversation.messages.push({ role: 'assistant', content: '' })
    const index = conversation.messages.length - 1
    await readStream(response, conversation.messages, index)
    if (!conversation.messages[index].content) {
      conversation.messages[index].content = '（没有收到内容，再试一次？）'
    }
  } catch (failure) {
    conversation.messages.push({ role: 'assistant', content: `❌ ${failure.message}` })
  } finally {
    isLoading.value = false
  }
}

function fetchChat(token, characterId, prompt, images) {
  return fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      token,
      character_id: characterId,
      prompt,
      images_base64: images.length > 0 ? images : null
    })
  })
}

async function readStream(response, messages, index) {
  await readSSEStream(response, (event) => {
    if (event.type === 'delta' && event.text) {
      messages[index].content += event.text
      scrollToBottom()
    } else if (event.type === 'final' && typeof event.text === 'string') {
      // 后端把动作旁白去掉后的最终文本：覆盖刚才流式显示的内容
      messages[index].content = event.text
      scrollToBottom()
    } else if (event.type === 'sticker' && event.codepoint) {
      messages[index].sticker = event
      scrollToBottom()
    } else if (event.type === 'error') {
      messages[index].content += `\n\n[系统错误: ${event.message}]`
    }
  })
}

// ---------------- 群聊 ----------------
function handleGroupKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    sendGroupMessage()
  }
}

async function scrollGroupBottom() {
  await nextTick()
  if (groupContainer.value) groupContainer.value.scrollTop = groupContainer.value.scrollHeight
}

function groupTokenKey() {
  return `novel:gtoken:${myName.value}`
}

async function ensureGroupToken() {
  if (group.token) return group.token
  const cached = localStorage.getItem(groupTokenKey())
  if (cached) {
    group.token = cached
    return cached
  }
  const session = await api('/api/session', {
    method: 'POST',
    body: { display_name: myName.value }
  })
  group.token = session.token
  localStorage.setItem(groupTokenKey(), session.token)
  return session.token
}

async function loadGroupMembers() {
  if (!group.shelfId) return
  group.loading = true
  group.error = ''
  try {
    const data = await api(`/api/shelves/${group.shelfId}/members`)
    group.members = data.members || []
  } catch (failure) {
    group.error = failure.message
  } finally {
    group.loading = false
  }
}

async function openGroup() {
  const shelfId = activeBook.value?.shelf_id
  if (!shelfId) {
    alert('这本书还没有归到书架，暂时不能进群')
    return
  }
  group.loading = true
  try {
    const shelf = shelves.value.find((item) => item.id === shelfId)
    await enterGroup(shelfId, shelf?.title || activeBook.value?.title || '群聊', activeBook.value)
  } catch (failure) {
    alert(failure.message)
  } finally {
    group.loading = false
  }
}

// ---------------- 群聊历史 / 群聊信息 ----------------
async function loadGroupHistory() {
  if (!group.shelfId) return
  try {
    const token = await ensureGroupToken()
    const query = `?token=${encodeURIComponent(token)}&limit=80`
    const data = await api(`/api/shelves/${group.shelfId}/history${query}`)
    group.messages = (data.turns || []).map(turnToGroupMessage)
    scrollGroupBottom()
  } catch (failure) {
    // 历史拉不到不该挡着聊天
  }
}

function turnToGroupMessage(turn) {
  if (turn.role === 'user') {
    return { kind: 'user', content: turn.content || '', mentions: turn.mentions || [] }
  }
  const member =
    group.members.find((item) => item.character_id === turn.character_id) ||
    group.members.find((item) => item.name === turn.speaker_name)
  return {
    kind: 'character',
    character_id: turn.character_id || '',
    name: turn.speaker_name || member?.name || '角色',
    avatar: member?.avatar || '🎭',
    avatar_url: member?.avatar_url || '',
    content: turn.content || '',
    mentions: turn.mentions || [],
    sticker: null
  }
}

//: 清空我在这个书架的群聊记录（只删流水，不删角色的长期记忆）
async function clearGroupHistory() {
  const tip = [
    '清空这个群里的聊天记录？',
    '',
    '· 只清空你自己的聊天流水（界面上看到的那些）',
    '· 角色已经记住的长期记忆不受影响',
    '· 不可恢复'
  ].join('\n')
  if (!confirm(tip)) return
  try {
    const token = await ensureGroupToken()
    const result = await api(
      `/api/shelves/${group.shelfId}/history?token=${encodeURIComponent(token)}`,
      { method: 'DELETE' }
    )
    group.messages = []
    closeGroupInfo()
    error.value = ''
    console.info(`已清空 ${result.removed} 条聊天记录`)
  } catch (failure) {
    groupInfo.error = failure.message
  }
}

function openGroupInfo() {
  groupInfo.expanded = false
  groupInfo.title = group.shelfTitle
  groupInfo.error = ''
  view.value = 'groupInfo'
  loadMergeCandidates()
}

//: 拉取「疑似同一个人」的候选（只读，点按钮才合并）
async function loadMergeCandidates() {
  if (!group.shelfId) return
  mergePanel.loading = true
  mergePanel.error = ''
  try {
    const data = await api(`/api/shelves/${group.shelfId}/merge/candidates`)
    mergePanel.pairs = data.pairs || []
    mergePanel.learned = data.learned_count || 0
  } catch (failure) {
    mergePanel.error = failure.message
    mergePanel.pairs = []
  } finally {
    mergePanel.loading = false
  }
}

//: 用户拍板一对：same=true 合并，false 记成"不是同一人"（以后不再问）
async function resolveMerge(pair, same) {
  if (mergePanel.busy) return
  const tip = same
    ? `把「${pair.left.name}」和「${pair.right.name}」合并成一个角色？\n\n出场多的那个会保留，另一个的记忆会并过去。`
    : `确认「${pair.left.name}」和「${pair.right.name}」不是同一个人？\n\n记下之后不会再拿这两个来问你。`
  if (!confirm(tip)) return

  mergePanel.busy = pair.left.id
  mergePanel.error = ''
  try {
    await api(`/api/shelves/${group.shelfId}/merge/resolve`, {
      method: 'POST',
      body: { left_id: pair.left.id, right_id: pair.right.id, same }
    })
    await loadGroupMembers()
    await loadMergeCandidates()
  } catch (failure) {
    mergePanel.error = failure.message
  } finally {
    mergePanel.busy = ''
  }
}

function closeGroupInfo() {
  view.value = 'group'
}

//: 成员网格：默认 5 列 × 3 行；超出时最后一格换成「更多」
const visibleMembers = computed(() => {
  const all = group.members
  if (groupInfo.expanded) return all
  const capacity = MEMBER_COLS * MEMBER_ROWS
  if (all.length <= capacity) return all
  return all.slice(0, capacity - 1)
})

const hasMoreMembers = computed(
  () => !groupInfo.expanded && group.members.length > MEMBER_COLS * MEMBER_ROWS
)

async function saveGroupTitle() {
  const title = groupInfo.title.trim()
  if (!title) {
    groupInfo.error = '群聊名称不能为空'
    return
  }
  groupInfo.saving = true
  groupInfo.error = ''
  try {
    await api(`/api/shelves/${group.shelfId}`, { method: 'PATCH', body: { title } })
    group.shelfTitle = title
    const shelf = shelves.value.find((item) => item.id === group.shelfId)
    if (shelf) shelf.title = title
  } catch (failure) {
    groupInfo.error = failure.message
  } finally {
    groupInfo.saving = false
  }
}

async function toggleMute(member) {
  const next = member.status === 'muted' ? 'active' : 'muted'
  try {
    await api(`/api/shelves/${group.shelfId}/members/${member.character_id}`, {
      method: 'PATCH',
      body: { status: next }
    })
    member.status = next
    member.state = next === 'muted' ? 'muted' : 'idle'
  } catch (failure) {
    group.error = failure.message
  }
}

function parseLocalMentions(text) {
  if (/@(全体成员|所有人|全体|all)/i.test(text)) return ['all']
  const found = []
  for (const member of group.members) {
    if (member.name && text.includes(`@${member.name}`)) found.push(member.name)
  }
  return found
}

async function sendGroupMessage() {
  const text = group.input.trim()
  if (!text || group.loading) return
  if (!group.members.length) {
    alert('这个书架还没有角色，先把书加进来')
    return
  }

  group.messages.push({ kind: 'user', content: text, mentions: parseLocalMentions(text) })
  group.input = ''
  group.loading = true
  scrollGroupBottom()

  try {
    let token = await ensureGroupToken()
    const request = (authToken) =>
      fetch(`/api/shelves/${group.shelfId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: authToken, prompt: text })
      })

    let response = await request(token)
    if (response.status === 401) {
      localStorage.removeItem(groupTokenKey())
      group.token = ''
      token = await ensureGroupToken()
      response = await request(token)
    }

    if (!response.ok) {
      let detail = `请求失败（HTTP ${response.status}）`
      try {
        const data = await response.json()
        if (data?.detail) detail = data.detail
      } catch (parseError) {
        /* ignore */
      }
      throw new Error(detail)
    }

    await readGroupStream(response)
  } catch (failure) {
    group.messages.push({ kind: 'system', content: `❌ ${failure.message}` })
  } finally {
    group.loading = false
    await loadGroupMembers()
  }
}

async function readGroupStream(response) {
  //: character_id -> 消息下标（同一个人在后续轮次会新建一条消息）
  const active = new Map()

  await readSSEStream(response, (event) => {
    if (event.type === 'speaker_start') {
      group.messages.push({
        kind: 'character',
        character_id: event.character_id,
        name: event.name,
        avatar: event.avatar || '🎭',
        avatar_url: event.avatar_url || '',
        content: '',
        mentions: [],
        typing: true,
        forced: Boolean(event.forced)
      })
      active.set(event.character_id, group.messages.length - 1)
      scrollGroupBottom()
    } else if (event.type === 'delta') {
      const index = active.get(event.character_id)
      if (index !== undefined) {
        group.messages[index].content += event.text
        scrollGroupBottom()
      }
    } else if (event.type === 'speaker_end') {
      const index = active.get(event.character_id)
      if (index !== undefined) {
        group.messages[index].typing = false
        group.messages[index].mentions = event.mentions || []
        group.messages[index].sticker = event.sticker || null
        if (event.text) group.messages[index].content = event.text
      }
      active.delete(event.character_id)
    } else if (event.type === 'system') {
      group.messages.push({ kind: 'system', content: event.text })
      scrollGroupBottom()
    } else if (event.type === 'error') {
      group.messages.push({ kind: 'system', content: `⚠️ ${event.message}` })
      scrollGroupBottom()
    }
  })
}

// ---------------- 生命周期 ----------------
onMounted(async () => {
  localStorage.setItem(NAME_KEY, myName.value)
  await loadHealth()
  await loadBooks()
  // 默认直接进群聊（记住上次进的是哪个书架）；没有书架就停在书架页引导上传
  if (shelves.value.length) {
    const lastId = localStorage.getItem(LAST_SHELF_KEY)
    const target = shelves.value.find((item) => item.id === lastId) || shelves.value[0]
    await openShelfChat(target)
  }
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<style scoped>
.app-container {
  --bg: #ffffff;
  --bg-soft: #f7f7f9;
  --text: #111827;
  --text-soft: #6b7280;
  --border: #e5e7eb;
  --primary: #4f46e5;
  --primary-soft: #eef2ff;
  --danger: #dc2626;

  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-soft);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif;
}

/* ---------- 顶栏 ---------- */
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 28px;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
}
.brand { display: flex; align-items: center; gap: 12px; cursor: pointer; }
.brand-icon { font-size: 26px; }
.brand h1 { margin: 0; font-size: 18px; }
.brand p { margin: 2px 0 0; font-size: 12px; color: var(--text-soft); }
.name-field { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-soft); }
.name-field input {
  width: 110px; padding: 7px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 8px; outline: none;
}
.name-field input:focus { border-color: var(--primary); }

.banner { margin: 12px 28px 0; padding: 10px 14px; border-radius: 10px; font-size: 13px; }
.banner.warn { background: #fffbeb; color: #92400e; border: 1px solid #fde68a; }
.banner.error { background: #fef2f2; color: var(--danger); border: 1px solid #fecaca; }
.banner code { background: rgba(0, 0, 0, 0.06); padding: 1px 5px; border-radius: 4px; }

/* ---------- 通用页面 ---------- */
.page { flex: 1; padding: 24px 28px 40px; max-width: 1080px; width: 100%; margin: 0 auto; }
.page-head { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 20px; }
.page-head h2 { margin: 0; font-size: 18px; }
.sub { margin: 4px 0 0; font-size: 12px; color: var(--text-soft); }
.empty { color: var(--text-soft); font-size: 14px; text-align: center; padding: 40px 0; }
.error-text { color: var(--danger); font-size: 13px; margin: 8px 0 0; }

button {
  font-family: inherit; font-size: 13px; cursor: pointer;
  border: 1px solid var(--border); background: var(--bg);
  border-radius: 8px; padding: 8px 14px; color: var(--text);
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}
button:disabled { opacity: 0.5; cursor: not-allowed; }
button.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
button.primary:not(:disabled):hover { background: #4338ca; }
button.ghost { background: transparent; }
button.ghost:not(:disabled):hover { background: var(--bg-soft); }
button.danger { color: var(--danger); border-color: #fecaca; }
button.danger:not(:disabled):hover { background: #fef2f2; }
button.small { padding: 5px 10px; font-size: 12px; }

/* ---------- 书架 ---------- */
.upload-card {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 6px; padding: 34px 20px; margin-bottom: 22px; cursor: pointer;
  background: var(--bg); border: 2px dashed #d1d5db; border-radius: 16px;
  transition: border-color 0.2s, background 0.2s;
}
.upload-card:hover { border-color: var(--primary); background: var(--primary-soft); }
.upload-card.busy { pointer-events: none; opacity: 0.7; }
.upload-icon { font-size: 30px; color: var(--primary); }
.upload-title { font-size: 15px; font-weight: 600; }
.upload-hint { font-size: 12px; color: var(--text-soft); }

.book-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }
.book-card {
  display: flex; flex-direction: column; justify-content: space-between;
  background: var(--bg); border: 1px solid var(--border); border-radius: 14px;
  padding: 16px; transition: box-shadow 0.2s, transform 0.2s;
}
.book-card:hover { box-shadow: 0 8px 20px -12px rgba(17, 24, 39, 0.35); }
.book-main { cursor: pointer; }
.book-card h3 { margin: 0 0 6px; font-size: 15px; }
.book-card .meta { margin: 0; font-size: 12px; color: var(--text-soft); }
.progress { margin: 12px 0 6px; height: 6px; background: #eef0f4; border-radius: 999px; overflow: hidden; }
.progress-bar { height: 100%; background: var(--primary); border-radius: 999px; transition: width 0.4s; }
.book-card .status { margin: 8px 0 0; font-size: 12px; color: var(--text-soft); }
.book-card .status.ready { color: #047857; }
.book-card .status.failed { color: var(--danger); }
.book-actions { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }

/* ---------- 角色 ---------- */
.character-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.character-card {
  display: flex; gap: 12px; align-items: flex-start; text-align: left;
  background: var(--bg); border: 1px solid var(--border); border-radius: 14px;
  padding: 16px; cursor: pointer; transition: box-shadow 0.2s, border-color 0.2s;
}
.character-card:hover { border-color: #c7d2fe; box-shadow: 0 8px 20px -12px rgba(79, 70, 229, 0.45); }
.character-card.add {
  flex-direction: column; align-items: center; justify-content: center; gap: 6px;
  border-style: dashed; color: var(--text-soft); min-height: 130px;
}
.character-card.add .plus { font-size: 24px; color: var(--primary); }
.character-card.add .hint { font-size: 11px; }
.character-body { flex: 1; min-width: 0; }
.character-body h3 { margin: 0 0 4px; font-size: 15px; display: flex; align-items: center; gap: 6px; }
.character-body .role { margin: 0 0 8px; font-size: 12px; color: var(--text-soft); }
.character-body .background {
  margin: 8px 0 0; font-size: 12px; color: var(--text-soft);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.tag { font-size: 10px; padding: 1px 6px; border-radius: 999px; font-weight: 500; }
.tag.user { background: var(--primary-soft); color: var(--primary); }
.tag.custom { background: #ecfdf5; color: #047857; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chips.center { justify-content: center; }
.chip { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--bg-soft); color: var(--text-soft); }
.card-actions { display: flex; flex-direction: column; gap: 6px; }

/* ---------- 聊天 ---------- */
.chat-page { display: flex; flex-direction: column; padding-bottom: 0; max-width: 860px; }
.chat-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding-bottom: 12px; border-bottom: 1px solid var(--border);
}
.chat-who { display: flex; align-items: center; gap: 10px; }
.chat-who h3 { margin: 0; font-size: 15px; }

.avatar {
  width: 36px; height: 36px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  background: var(--primary-soft); font-size: 19px;
}
.avatar.big { width: 46px; height: 46px; font-size: 24px; }
.avatar.me { background: #fce7f3; }

.chat-container { flex: 1; overflow-y: auto; padding: 20px 4px; min-height: 320px; }
.chat-messages { display: flex; flex-direction: column; gap: 20px; }

.intro {
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  text-align: center; padding: 24px 16px; color: var(--text-soft);
}
.intro h3 { margin: 0; font-size: 16px; color: var(--text); }
.intro p { margin: 0; font-size: 13px; max-width: 460px; line-height: 1.6; }
.intro .hint { font-size: 12px; }

.message-wrapper { display: flex; gap: 12px; width: 100%; }
.message-wrapper.user { justify-content: flex-end; }
.message-content { max-width: 78%; }
.message-bubble {
  padding: 12px 16px; border-radius: 12px; font-size: 15px; line-height: 1.7;
  color: #1f2937; word-break: break-word;
}
.message-wrapper.assistant .message-bubble { background: var(--bg); border: 1px solid var(--border); }
.message-wrapper.user .message-bubble { background: var(--primary-soft); border-top-right-radius: 4px; }
.message-wrapper.assistant .message-bubble :deep(p) { margin: 6px 0; }
.system-message { width: 100%; text-align: center; font-size: 12px; color: var(--text-soft); }

.image-placeholder {
  display: flex; align-items: center; gap: 8px;
  background: var(--bg-soft); padding: 8px 12px; border-radius: 8px; font-size: 13px;
}

/* Markdown */
:deep(.message-bubble p) { margin: 8px 0; }
:deep(.message-bubble p:first-child) { margin-top: 0; }
:deep(.message-bubble p:last-child) { margin-bottom: 0; }
:deep(.message-bubble ul), :deep(.message-bubble ol) { padding-left: 22px; margin: 8px 0; }
:deep(.message-bubble li) { margin-bottom: 4px; }
:deep(.message-bubble blockquote) {
  border-left: 3px solid var(--primary); margin: 10px 0; padding: 6px 12px;
  background: #f8fafc; border-radius: 0 8px 8px 0; color: var(--text-soft);
}
:deep(.message-bubble pre) { background: #1e293b; color: #f8fafc; padding: 12px; border-radius: 8px; overflow-x: auto; }
:deep(.message-bubble code) { background: #f1f5f9; color: #db2777; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
:deep(.message-bubble pre code) { background: transparent; color: inherit; padding: 0; }

.chat-input-wrapper { padding: 12px 0 20px; border-top: 1px solid var(--border); }
.chat-input-box {
  display: flex; align-items: flex-end; gap: 10px;
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 12px; padding: 8px 12px; transition: border-color 0.2s, box-shadow 0.2s;
}
.chat-input-box:focus-within { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.12); }
.chat-input-box textarea {
  flex: 1; border: none; outline: none; resize: none; font-family: inherit;
  font-size: 14px; line-height: 1.6; padding: 8px 0; max-height: 180px; background: transparent;
}
.upload-btn { cursor: pointer; font-size: 18px; padding: 4px; border-radius: 6px; color: var(--text-soft); }
.upload-btn:hover { background: var(--bg-soft); }
.send-btn {
  background: var(--primary); border: none; color: #fff;
  padding: 9px 18px; border-radius: 8px; font-weight: 500;
}
.send-btn:disabled { background: #cbd5e1; color: #f8fafc; }
.footer-text { margin: 10px 0 0; font-size: 11px; color: var(--text-soft); text-align: center; }

.pending-images { display: flex; gap: 8px; margin-bottom: 8px; overflow-x: auto; }
.preview-item { position: relative; width: 58px; height: 58px; border-radius: 8px; overflow: hidden; border: 1px solid var(--border); }
.preview-item img { width: 100%; height: 100%; object-fit: cover; }
.remove-btn {
  position: absolute; top: 2px; right: 2px; width: 18px; height: 18px;
  border-radius: 50%; border: none; background: rgba(0, 0, 0, 0.55); color: #fff;
  font-size: 12px; line-height: 1; display: flex; align-items: center; justify-content: center;
}

.typing span {
  display: inline-block; width: 6px; height: 6px; border-radius: 50%;
  background: #9ca3af; margin-right: 4px; animation: bounce 1.4s infinite ease-in-out both;
}
.typing span:nth-child(1) { animation-delay: -0.32s; }
.typing span:nth-child(2) { animation-delay: -0.16s; }
@keyframes bounce { 0%, 80%, 100% { transform: scale(0); } 40% { transform: scale(1); } }

/* ---------- 弹窗 ---------- */
.modal-mask {
  position: fixed; inset: 0; background: rgba(17, 24, 39, 0.45);
  display: flex; align-items: center; justify-content: center; padding: 20px; z-index: 20;
}
.modal {
  width: min(560px, 100%); max-height: 90vh; overflow-y: auto;
  background: var(--bg); border-radius: 16px; padding: 22px;
}
.modal h3 { margin: 0 0 4px; font-size: 17px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }
.form-grid label { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--text-soft); }
.form-grid label.wide { grid-column: 1 / -1; }
.form-grid label.check { flex-direction: row; align-items: center; gap: 8px; }
.form-grid input:not([type='checkbox']),
.form-grid textarea {
  font-family: inherit; font-size: 13px; padding: 8px 10px;
  border: 1px solid var(--border); border-radius: 8px; outline: none; resize: vertical;
}
.form-grid input:focus, .form-grid textarea:focus { border-color: var(--primary); }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }

/* ---------- 角色详情 / 状态 ---------- */
.status-modal { width: min(620px, 100%); }
.status-head { display: flex; gap: 14px; align-items: center; }
.status-head h3 { display: flex; align-items: center; gap: 8px; }
.status-block { margin-top: 18px; padding-top: 14px; border-top: 1px dashed var(--border); }
.status-block h4 {
  margin: 0 0 10px; font-size: 12px; font-weight: 600;
  letter-spacing: 0.06em; color: var(--text-soft);
}
.status-mood { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.mood-badge {
  padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
  background: var(--primary-soft); color: var(--primary);
}
.mood-text { font-size: 13px; }
.status-line { display: flex; gap: 12px; margin: 7px 0; font-size: 13px; line-height: 1.65; }
.status-line strong { flex: 0 0 68px; font-weight: 500; color: var(--text-soft); }
.chat-who.clickable {
  cursor: pointer; border-radius: 12px; padding: 4px 8px; margin-left: -8px;
  transition: background 0.15s;
}
.chat-who.clickable:hover { background: var(--primary-soft); }

/* ---------- 群聊 ---------- */
.group-members {
  display: flex; flex-wrap: wrap; gap: 8px;
  padding: 10px 4px; border-bottom: 1px solid var(--border);
}
.group-member {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 10px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--border); background: var(--bg);
  font-size: 12px; color: var(--text); transition: border-color 0.15s, opacity 0.15s;
}
.group-member:hover { border-color: var(--primary); }
.group-member.muted { opacity: 0.45; }
.avatar.small { width: 22px; height: 22px; font-size: 12px; }
.group-member-name { font-weight: 500; }
.group-member-state { color: var(--text-soft); }
.message-speaker {
  margin: 0 0 4px; font-size: 12px; color: var(--text-soft);
  display: flex; align-items: center; gap: 6px;
}
.typing-dot { font-size: 11px; color: var(--text-soft); }
.message-mentions { margin: 4px 0 0; font-size: 11px; color: var(--primary); }
.head-actions { display: flex; gap: 8px; align-items: center; }

/* ---------- 书架页（书库 → 书架 → 书）---------- */
.shelf-toolbar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-bottom: 18px;
}
.upload-card.compact {
  flex: 1; min-width: 320px; padding: 16px 20px;
  flex-direction: row; align-items: center; gap: 10px;
}
.upload-card.compact .upload-title { font-size: 14px; }
.shelf-picker { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-soft); }
.shelf-picker select {
  padding: 8px 10px; border-radius: 10px; border: 1px solid var(--border);
  background: var(--bg); font-size: 13px; color: var(--text);
}
.shelf-list { display: flex; flex-direction: column; gap: 16px; }
.shelf-card {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 14px; padding: 16px 18px;
}
.shelf-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.shelf-title h3 { margin: 0; font-size: 16px; }
.shelf-title .meta { margin: 4px 0 0; font-size: 12px; color: var(--text-soft); }
.shelf-actions { display: flex; gap: 8px; flex-shrink: 0; }
.shelf-books { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 10px; }
.shelf-book {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px; border-radius: 10px; font-size: 13px;
}
.shelf-book.ready { cursor: pointer; }
.shelf-book.ready:hover { background: var(--bg-soft); }
.book-name { font-weight: 500; flex-shrink: 0; }
.book-meta { color: var(--text-soft); font-size: 12px; }
.shelf-book .status { margin-left: auto; font-size: 12px; }
.shelf-book .status.ready { color: #059669; }
.shelf-book .status.failed { color: var(--danger); }
.shelf-book .status.extracting { color: var(--primary); }
.empty-mini { margin: 6px 0 0; font-size: 12px; color: var(--text-soft); }
.progress-mini { width: 60px; height: 4px; border-radius: 2px; background: var(--border); overflow: hidden; }
.progress.inline {
  margin: 0; width: 72px; max-width: 72px; height: 5px;
  background: var(--border); border-radius: 999px; overflow: hidden; position: relative;
}
.progress.inline::after {
  content: ''; position: absolute; inset: 0; background: var(--primary); border-radius: 999px;
}
.ghost.tiny { padding: 3px 8px; font-size: 12px; }

/* ---------- 群聊信息（独立界面） ---------- */
.info-panel {
  margin-top: 6px; padding: 18px 20px; max-width: 760px;
  background: var(--bg); border: 1px solid var(--border); border-radius: 14px;
}
.group-info { max-width: 520px; }
.info-field { display: flex; align-items: center; gap: 8px; margin: 14px 0 4px; font-size: 13px; }
.info-field span { color: var(--text-soft); white-space: nowrap; }
.info-field input {
  flex: 1; padding: 8px 10px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--bg); font-size: 14px; color: var(--text);
}
.info-sub { margin: 18px 0 10px; font-size: 13px; color: var(--text-soft); font-weight: 500; }
.member-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
.member-cell {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 10px 4px; border-radius: 12px; cursor: pointer;
  border: 1px solid transparent; background: var(--bg-soft);
  font-size: 12px; color: var(--text);
}
.member-cell:hover { border-color: var(--primary); background: var(--primary-soft); }
.member-cell .avatar { width: 40px; height: 40px; font-size: 20px; }
.member-cell-name {
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.member-cell.more .more-icon { display: flex; align-items: center; justify-content: center; letter-spacing: 2px; }
.member-cell.offline { opacity: 0.45; }
.info-actions { display: flex; gap: 8px; margin-top: 18px; flex-wrap: wrap; }

/* ---------- 待确认的合并 ---------- */
.merge-hint { float: right; font-weight: 400; font-size: 11px; color: var(--text-soft); }
.merge-row {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 0; border-top: 1px dashed var(--border); font-size: 13px;
}
.merge-row:first-of-type { border-top: none; }
.merge-names { min-width: 190px; }
.merge-vs { margin: 0 8px; color: var(--text-soft); }
.merge-meta {
  flex: 1; display: flex; gap: 12px; flex-wrap: wrap;
  font-size: 12px; color: var(--text-soft);
}
.merge-actions { display: flex; gap: 6px; }

/* ---------- 手动合并 ---------- */
.merge-modal { max-width: 460px; }
.merge-list {
  display: flex; flex-direction: column; gap: 6px;
  max-height: 300px; overflow-y: auto; margin: 14px 0;
}
.merge-item {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; border-radius: 10px; cursor: pointer;
  border: 1px solid var(--border); background: var(--bg);
  font-size: 13px; text-align: left; color: var(--text);
}
.merge-item:hover { border-color: var(--primary); }
.merge-item.active { border-color: var(--primary); background: var(--primary-soft); }
.merge-name { font-weight: 500; }

/* ---------- 贴纸（Twemoji）---------- */
.sticker {
  display: block; width: 44px; height: 44px;
  margin-top: 6px; object-fit: contain;
}
.sticker.emoji-only { font-size: 34px; line-height: 44px; width: auto; }

/* ---------- 记忆库状态 ---------- */
.banner.rag {
  background: #f5f3ff; color: #4c1d95; border: 1px solid #ddd6fe;
  font-size: 12px; padding: 8px 14px; border-radius: 10px; margin: 0 28px 8px;
}
.rag-hint { color: #6d28d9; opacity: 0.8; }

/* ---------- 头像 ---------- */
.avatar.img { object-fit: cover; overflow: hidden; padding: 0; border: 1px solid var(--border); }
.avatar.big.img { border-radius: 50%; }
.avatar-picker { margin-top: 18px; padding-top: 14px; border-top: 1px dashed var(--border); }
.avatar-picker-head {
  display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;
  font-size: 12px; color: var(--text-soft);
}
.avatar-grid { display: flex; flex-wrap: wrap; gap: 10px; }
.avatar-choice {
  width: 64px; height: 64px; padding: 0; border-radius: 12px;
  border: 2px solid transparent; background: var(--bg-soft); overflow: hidden;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; color: var(--text-soft);
}
.avatar-choice img { width: 100%; height: 100%; object-fit: cover; }
.avatar-choice.active { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.15); }
.avatar-choice:hover { border-color: #c7d2fe; }
.audit-btn { margin-left: auto; white-space: nowrap; }

/* ---------- 滚动条 ---------- */
.chat-container::-webkit-scrollbar, .modal::-webkit-scrollbar { width: 8px; }
.chat-container::-webkit-scrollbar-thumb, .modal::-webkit-scrollbar-thumb {
  background: #d1d5db; border-radius: 4px;
}

/* ---------- 小屏 ---------- */
@media (max-width: 720px) {
  .topbar { flex-direction: column; align-items: flex-start; gap: 10px; }
  .page { padding: 16px; }
  .book-grid, .character-grid { grid-template-columns: 1fr; }
  .form-grid { grid-template-columns: 1fr; }
  .message-content { max-width: 88%; }
}
</style>
