// 临时 SSR 冒烟：灌入假数据后把四个视图 + 三个浮层各渲染一遍。
// 模板里任何未定义绑定、null 解引用都会在这里直接抛错。
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'

import App from '../src/App.vue'
import { useApp } from '../src/stores/app.js'

const character = {
  id: 'char_1',
  name: '沈砚',
  role: '剑客',
  avatar: '🗡️',
  avatar_url: '',
  aliases: ['小沈'],
  personality: ['冷淡'],
  speech_style: '简短',
  background: '出身寒门',
  source: 'novel',
  appearances: 12,
  is_user_persona: false
}
const member = {
  character_id: 'char_1',
  name: '沈砚',
  avatar: '🗡️',
  avatar_url: '',
  status: 'online',
  state: 'idle',
  muted: false
}

function seed(app) {
  app.health.value = {
    api_key_configured: true,
    rag: { backend: 'vector', model_exists: true, model_path: 'models/bge' }
  }
  app.shelves.value = [{ id: 'shelf_1', title: '龙族大家庭', book_count: 2, character_count: 3 }]
  app.books.value = [{
    id: 'book_1', title: '龙族Ⅰ', shelf_id: 'shelf_1', status: 'ready',
    character_count: 3, char_count: 318602, progress: 100, stage: '完成'
  }]
  app.uploadShelfId.value = 'shelf_1'

  app.group.shelfId = 'shelf_1'
  app.group.shelfTitle = '龙族大家庭'
  app.group.members = [member]
  const tinyPng =
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

  app.group.messages = [
    { kind: 'user', content: '给你看张图', images: [tinyPng], at: '2026-11-11T10:30:00+08:00' },
    { kind: 'system', content: '系统提示' },
    {
      kind: 'character', character_id: 'char_1', name: '沈砚', avatar: '🗡️',
      avatar_url: '', content: '嗯。', mentions: ['苏晚'], typing: false,
      sticker: { url: 'https://example.com/a.svg', emoji: '😀' }
    }
  ]

  app.groupInfo.title = '龙族大家庭'
  app.mergePanel.pairs = [{
    left: { id: 'a', name: '诺诺', role: '', book_id: 'b1', appearances: 3 },
    right: { id: 'b', name: '陈墨瞳', role: '', book_id: 'b2', appearances: 5 },
    score: '0.62',
    common: ['路明非']
  }]
  app.mergePanel.learned = 7

  app.activeCharacter.value = character
  app.conversations[character.id] = {
    token: null,
    messages: [
      { role: 'user', content: '你好', at: '2026-11-11T10:30:00+08:00' },
      { role: 'user', content: '', isImage: true, fileName: 'a.png', images: [tinyPng] },
      { role: 'assistant', content: '嗯。', sticker: { url: 'https://example.com/a.svg', emoji: '😀' } },
      { role: 'system', content: '系统' }
    ]
  }
}

export async function renderAll() {
  const app = useApp()
  seed(app)

  const out = {}
  for (const view of ['shelf', 'group', 'groupInfo', 'chat']) {
    app.view.value = view
    out[view] = await renderToString(createSSRApp(App))
  }

  app.merge.open = true
  app.editor.open = true
  app.editor.mode = 'edit'
  app.editor.form.name = '沈砚'
  app.editor.candidates = [{ url: 'https://example.com/1.jpg', source: 'bangumi' }]
  app.status.open = true
  app.status.data = {
    character: {
      ...character,
      book_title: '龙族Ⅰ',
      shelf_title: '龙族大家庭',
      relationships: [{ with: '苏晚', relation: '同门' }]
    },
    status: {
      mood: '平静',
      mood_text: '还好',
      attitude_text: '有点在意',
      turns: 3,
      last_active: '2026-01-01T10:00:00Z',
      presence: '在线',
      merged_children: [{ id: 'c2', name: '小沈' }],
      memory: { total: 9, by_source: { book_1: 9 }, summary: '摘要' }
    },
    member
  }
  out.modals = await renderToString(createSSRApp(App))

  return out
}

