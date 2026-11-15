/** 应用状态与动作的唯一来源。
 *
 * 组件里 ``const { view, group, openShelfChat } = useApp()`` 拿到的就是同一份
 * ref / reactive 对象，所以模板里直接写名字即可，不需要加前缀。
 */

import { computed, nextTick, reactive, ref } from 'vue'

import { api } from '../api/client.js'
import {
  avatarSrc,
  formatCount,
  formatTime,
  groupMentionText,
  memberStateText,
  statusText
} from '../utils/format.js'
import { renderMarkdown } from '../utils/markdown.js'
import { readSSEStream } from '../utils/sse.js'

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
  error: '',
  //: 这个书架里的所有对话（左侧列表）
  conversations: [],
  //: 当前对话 id
  conversationId: '',
  conversationsLoading: false
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

//: 群聊要显示的消息（被聚合节点判定重复的会 hidden 掉）
const visibleGroupMessages = computed(() => group.messages.filter((item) => !item.hidden))

//: 当前会话对象（含 turnId / canResume，断流续跑要用）
const currentConversation = computed(() => {
  const id = activeCharacter.value?.id
  if (!id) return null
  return conversations[id] || null
})

// ---------------- 请求封装 ----------------
async function loadHealth() {
  try {
    health.value = await api('/api/health')
    // 把后端版本打到控制台：和 git log 对不上就说明前端是旧缓存
    const build = health.value?.build || {}
    console.info(`[NovelChat] 后端 ${build.commit || '?'} / 启动于 ${build.started_at || '?'}`)
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
  pendingImages.value = []
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
  await ensureConversation()
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
  pendingImages.value = []
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
      content: turn.content || '',
      images: turn.images || [],
      at: turn.created_at || null
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

/** 从「角色详情」直接进单聊 / 编辑：先把角色取出来，再关面板。
 *
 * 以前模板写的是 ``closeStatus(); openChat(status.data.character)``——
 * closeStatus 会把 status.data 置空，第二个表达式立刻抛错，按钮就"点了没反应"。
 */
function chatFromStatus() {
  const character = status.data?.character
  if (!character) return undefined
  closeStatus()
  return openChat(character)
}

function editFromStatus() {
  const character = status.data?.character
  if (!character) return undefined
  closeStatus()
  return openEditor(character)
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
    conversation.messages.push({
      role: 'user',
      content: '',
      isImage: true,
      fileName: image.fileName,
      images: [image.base64]
    })
    imagesPayload.push(image.base64)
  })
  conversation.messages.push({ role: 'user', content: userText, at: new Date().toISOString() })

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

    conversation.messages.push({ role: 'assistant', content: '', at: new Date().toISOString() })
    const index = conversation.messages.length - 1
    await readStream(response, conversation.messages, index, conversation)
    if (!conversation.messages[index].content) {
      conversation.messages[index].content = '（没有收到内容，再试一次？）'
    }
  } catch (failure) {
    conversation.messages.push({ role: 'assistant', content: `❌ ${failure.message}` })
  } finally {
    isLoading.value = false
  }
}

/** 断流后从断点继续生成（不重复提问，也不重复落库）。 */
async function resumeChat() {
  const character = activeCharacter.value
  if (!character) return
  const conversation = conversations[character.id]
  if (!conversation || !conversation.turnId) return
  if (isLoading.value) return

  // 把上一轮没写完的助手气泡清空，接着往里写
  const last = conversation.messages[conversation.messages.length - 1]
  if (last && last.role === 'assistant') last.content = ''
  else conversation.messages.push({ role: 'assistant', content: '' })

  const index = conversation.messages.length - 1
  isLoading.value = true
  try {
    const token = await ensureToken(character.id)
    const response = await fetchChat(token, character.id, '', [], {
      turnId: conversation.turnId,
      resume: true
    })
    await readStream(response, conversation.messages, index, conversation)
    if (!conversation.messages[index].content) {
      conversation.messages[index].content = '（没有收到内容，再试一次？）'
    }
  } catch (failure) {
    conversation.messages[index].content = `❌ ${failure.message}`
  } finally {
    isLoading.value = false
  }
}

function fetchChat(token, characterId, prompt, images, options = {}) {
  const body = {
    token,
    character_id: characterId,
    prompt,
    images_base64: images.length > 0 ? images : null
  }
  // 断点续跑：同一个 turn_id + resume=true，后端从断点接着生成
  if (options.turnId) body.turn_id = options.turnId
  if (options.resume) body.resume = true
  return fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  })
}

async function readStream(response, messages, index, conversation = null) {
  await readSSEStream(response, (event) => {
    if (event.type === 'turn' && conversation) {
      // 后端给这一轮分配的编号：断流后拿它续跑
      conversation.turnId = event.thread_id
      conversation.canResume = false
    } else if (event.type === 'delta' && event.text) {
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
      if (conversation && conversation.turnId) conversation.canResume = true
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

// ---------------- 对话列表（一个书架多个对话） ----------------

/** 拉会话列表；一个都没有就自动开一个 */
async function ensureConversation() {
  group.conversationsLoading = true
  try {
    const token = await ensureGroupToken()
    let list = await api(
      `/api/shelves/${group.shelfId}/conversations?token=${encodeURIComponent(token)}`
    )
    if (!Array.isArray(list)) list = []
    if (!list.length) {
      const created = await api(
        `/api/shelves/${group.shelfId}/conversations?token=${encodeURIComponent(token)}`,
        { method: 'POST' }
      )
      list = [created]
    }
    group.conversations = list
    // 保持当前对话；没有就取最近活跃的那个
    const current = list.find((item) => item.id === group.conversationId)
    group.conversationId = current ? current.id : list[0].id
  } catch (failure) {
    group.error = failure.message
  } finally {
    group.conversationsLoading = false
  }
  await loadGroupHistory()
}

/** 只刷新会话列表（标题/条数），不动当前消息 */
async function refreshConversations() {
  if (!group.shelfId) return
  try {
    const token = await ensureGroupToken()
    const list = await api(
      `/api/shelves/${group.shelfId}/conversations?token=${encodeURIComponent(token)}`
    )
    if (Array.isArray(list)) group.conversations = list
  } catch (failure) {
    // 刷新失败不影响聊天
  }
}

/** 新建对话并切过去 */
async function newConversation() {
  if (!group.shelfId) return
  try {
    const token = await ensureGroupToken()
    const created = await api(
      `/api/shelves/${group.shelfId}/conversations?token=${encodeURIComponent(token)}`,
      { method: 'POST' }
    )
    group.conversations = [created, ...group.conversations]
    group.conversationId = created.id
    group.messages = []
    group.error = ''
    scrollGroupBottom()
  } catch (failure) {
    group.error = failure.message
  }
}

/** 切到另一个对话 */
async function switchConversation(conversationId) {
  if (!conversationId || conversationId === group.conversationId) return
  group.conversationId = conversationId
  group.messages = []
  await loadGroupHistory()
}

/** 删掉一个对话 */
async function deleteConversation(conversationId) {
  if (!conversationId) return
  const target = group.conversations.find((item) => item.id === conversationId)
  const tip = [
    `删除对话「${target?.title || '这个对话'}」？`,
    '',
    '· 这个对话里的聊天记录会一起删除',
    '· 角色已经记住的长期记忆不受影响',
    '· 不可恢复'
  ].join('\n')
  if (!confirm(tip)) return
  try {
    const token = await ensureGroupToken()
    await api(
      `/api/conversations/${conversationId}?token=${encodeURIComponent(token)}`,
      { method: 'DELETE' }
    )
    group.conversations = group.conversations.filter((item) => item.id !== conversationId)
    if (group.conversationId === conversationId) {
      if (group.conversations.length) {
        group.conversationId = group.conversations[0].id
        await loadGroupHistory()
      } else {
        await ensureConversation()
      }
    }
  } catch (failure) {
    group.error = failure.message
  }
}

// ---------------- 群聊历史 / 群聊信息 ----------------
async function loadGroupHistory() {
  if (!group.shelfId) return
  try {
    const token = await ensureGroupToken()
    const query = `?token=${encodeURIComponent(token)}&limit=80`
      + `&conversation_id=${encodeURIComponent(group.conversationId || '')}`
    const data = await api(`/api/shelves/${group.shelfId}/history${query}`)
    group.messages = (data.turns || []).map(turnToGroupMessage)
    scrollGroupBottom()
  } catch (failure) {
    // 历史拉不到不该挡着聊天
  }
}

function turnToGroupMessage(turn) {
  if (turn.role === 'user') {
    return {
      kind: 'user',
      content: turn.content || '',
      mentions: turn.mentions || [],
      images: turn.images || [],
      at: turn.created_at || null
    }
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
    sticker: null,
    at: turn.created_at || null
  }
}

//: 清空群聊对话。
//: mode='distill'：先蒸馏成长期记忆再删流水（角色会记住聊过什么）
//: mode='purge'  ：连记忆一起删，彻底重来
async function clearGroupHistory(mode = 'distill') {
  const distill = mode !== 'purge'
  const tip = distill
    ? [
        '清空对话（保留记忆）？',
        '',
        `· 这 ${group.messages.length} 条对话会先被压缩成长期记忆`,
        '· 角色会记住你的喜好和状态，但界面上不再显示',
        '· 记忆写进角色档案，之后聊天会自动用到',
        '· 不可恢复'
      ].join('\n')
    : [
        '清空对话和记忆？',
        '',
        '· 这段对话会被直接删除（不蒸馏）',
        '· 这段对话蒸馏出的长期记忆一并删除',
        '· 角色会忘记这段时间聊过的内容',
        '· 不可恢复'
      ].join('\n')
  if (!confirm(tip)) return
  try {
    const token = await ensureGroupToken()
    const result = await api(
      `/api/shelves/${group.shelfId}/history?token=${encodeURIComponent(token)}`
      + `&mode=${distill ? 'distill' : 'purge'}`
      + `&conversation_id=${encodeURIComponent(group.conversationId || '')}`,
      { method: 'DELETE' }
    )
    group.messages = []
    closeGroupInfo()
    error.value = ''
    if (distill) {
      console.info(
        `已清空 ${result.removed} 条对话；已蒸馏 ${result.distilled || 0} 个角色的记忆`
      )
    } else {
      console.info(
        `已清空 ${result.removed} 条对话，删除 ${result.memory_removed || 0} 条记忆`
      )
    }
  } catch (failure) {
    groupInfo.error = failure.message
  }
}

//: 清空与某个角色的单聊（同样支持 distill / purge）
async function clearCharacterHistory(characterId, mode = 'distill') {
  const distill = mode !== 'purge'
  const tip = distill
    ? [
        '清空这段对话（保留记忆）？',
        '',
        '· 对话会先蒸馏成长期记忆，角色仍记得你',
        '· 界面上不再显示这些消息',
        '· 不可恢复'
      ].join('\n')
    : [
        '清空对话和记忆？',
        '',
        '· 这段对话直接删除（不蒸馏）',
        '· 蒸馏出的长期记忆一并删除',
        '· 角色会忘记这段时间',
        '· 不可恢复'
      ].join('\n')
  if (!confirm(tip)) return
  try {
    const token = await ensureGroupToken()
    const result = await api(
      `/api/characters/${characterId}/history?token=${encodeURIComponent(token)}`
      + `&mode=${distill ? 'distill' : 'purge'}`,
      { method: 'DELETE' }
    )
    if (currentConversation.value) currentConversation.value.messages = []
    if (distill) {
      console.info(`已清空 ${result.removed} 条对话，记忆已保存`)
    } else {
      console.info(
        `已清空 ${result.removed} 条对话，删除 ${result.memory_removed || 0} 条记忆`
      )
    }
  } catch (failure) {
    error.value = failure.message
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
  const images = pendingImages.value.slice()
  if ((!text && images.length === 0) || group.loading) return
  if (!group.members.length) {
    alert('这个书架还没有角色，先把书加进来')
    return
  }

  // 纯图片也要有个气泡，否则群里看不出用户发过东西
  const content = text || (images.length ? '（发了张图）' : '')
  group.messages.push({
    kind: 'user',
    content,
    mentions: parseLocalMentions(text),
    images: images.map((image) => image.base64),
    at: new Date().toISOString()
  })
  group.input = ''
  pendingImages.value = []
  group.loading = true
  scrollGroupBottom()

  try {
    let token = await ensureGroupToken()
    const request = (authToken) =>
      fetch(`/api/shelves/${group.shelfId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token: authToken,
          prompt: text,
          images_base64: images.map((image) => image.base64),
          // 发到当前选中的对话里；后端按这个 id 落流水
          conversation_id: group.conversationId || null
        })
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
  //: character_id -> 消息对象本身（不是下标：数组会变长、会被隐藏，
  //: 存下标会在 speaker_end 删除后让 speaker_drop 找不到人）
  const active = new Map()

  await readSSEStream(response, (event) => {
    if (event.type === 'speaker_drop') {
      // 聚合节点判定这条和别人重复：直接藏起来。
      // 此刻它已经 speaker_end 过了（active 里可能已删），所以按 character_id 回查，
      // 取**最后一条还没隐藏**的该角色消息。
      let target = active.get(event.character_id)
      if (!target) {
        for (let i = group.messages.length - 1; i >= 0; i -= 1) {
          const item = group.messages[i]
          if (item.kind === 'character' && item.character_id === event.character_id
            && !item.hidden) {
            target = item
            break
          }
        }
      }
      if (target) target.hidden = true
      active.delete(event.character_id)
      return
    }
    if (event.type === 'speaker_start') {
      const message = reactive({
        kind: 'character',
        character_id: event.character_id,
        name: event.name,
        avatar: event.avatar || '🎭',
        avatar_url: event.avatar_url || '',
        content: '',
        mentions: [],
        typing: true,
        forced: Boolean(event.forced),
        angle: event.angle || '',
        at: new Date().toISOString()
      })
      group.messages.push(message)
      active.set(event.character_id, message)
      scrollGroupBottom()
    } else if (event.type === 'delta') {
      const message = active.get(event.character_id)
      if (message) {
        message.content += event.text
        scrollGroupBottom()
      }
    } else if (event.type === 'speaker_end') {
      const message = active.get(event.character_id)
      if (message) {
        message.typing = false
        message.mentions = event.mentions || []
        message.sticker = event.sticker || null
        if (event.text) message.content = event.text
      }
      // 注意：这里**不能** active.delete —— speaker_drop 还要用
    } else if (event.type === 'system') {
      group.messages.push({ kind: 'system', content: event.text })
      scrollGroupBottom()
    } else if (event.type === 'error') {
      group.messages.push({ kind: 'system', content: `⚠️ ${event.message}` })
      scrollGroupBottom()
    } else if (event.type === 'done') {
      active.clear()
      // 首句会把对话自动命名，条数也变了——同步一下左侧列表
      refreshConversations()
    }
  })
}

// ---------------- 生命周期 ----------------

/** 启动：恢复身份 → 拉健康状态与书架 → 回到上次的群 */
export async function bootstrap() {
  localStorage.setItem(NAME_KEY, myName.value)
  await loadHealth()
  await loadBooks()
  if (shelves.value.length) {
    const lastId = localStorage.getItem(LAST_SHELF_KEY)
    const target = shelves.value.find((item) => item.id === lastId) || shelves.value[0]
    await openShelfChat(target)
  }
}

/** 停止抽取进度轮询（组件卸载时调用） */
export function stopPolling() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
}

const state = {
  chatFromStatus,
  editFromStatus,
  avatarSrc,
  formatCount,
  formatTime,
  groupMentionText,
  memberStateText,
  statusText,
  renderMarkdown,
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
  visibleGroupMessages,
  currentConversation,
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
  resumeChat,
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
  ensureConversation,
  refreshConversations,
  newConversation,
  switchConversation,
  deleteConversation,
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
  readGroupStream,
  bootstrap,
  stopPolling
}

/** 拿到全局单例状态（多次调用返回同一份）。 */
export function useApp() {
  return state
}
