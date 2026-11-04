// 静态校验前端：Vue SFC 能否编译 + 关键结构是否在位。
// 沙箱里 esbuild/vite 起不来（spawn EPERM），用 compiler-sfc 代替 build 做验证。
const fs = require('fs')
const path = require('path')

const root = path.resolve(__dirname, '..')
const { parse, compileScript, compileTemplate, compileStyle } = require(
  path.join(root, 'frontend/node_modules/@vue/compiler-sfc')
)
const filename = path.join(root, 'frontend/src/App.vue')
const source = fs.readFileSync(filename, 'utf8')

let failed = 0
const check = (name, ok, detail = '') => {
  if (!ok) failed += 1
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${ok || !detail ? '' : `  <- ${detail}`}`)
}

const { descriptor, errors } = parse(source, { filename })
check('SFC 解析', errors.length === 0, errors.map((e) => e.message).join('; '))
check('存在 template', Boolean(descriptor.template))
check('存在 script setup', Boolean(descriptor.scriptSetup))
check('存在 style', descriptor.styles.length > 0)

let bindings = {}
try {
  const script = compileScript(descriptor, { id: 'app' })
  bindings = script.bindings || {}
  check('script setup 编译', true)
} catch (error) {
  check('script setup 编译', false, error.message)
}

const tpl = compileTemplate({
  source: descriptor.template.content,
  filename,
  id: 'app',
  compilerOptions: { bindingMetadata: bindings }
})
check('template 编译', tpl.errors.length === 0, tpl.errors.map((e) => String(e)).join('; '))
check('模板产出了渲染函数', (tpl.code || '').includes('render'))

for (const [index, style] of descriptor.styles.entries()) {
  const result = compileStyle({ source: style.content, filename, id: `app-${index}`, scoped: style.scoped })
  check(`style #${index} 编译`, result.errors.length === 0, result.errors.map((e) => String(e)).join('; '))
}

// --- 模板用到的变量必须在 script 里声明 ---
const declared = new Set(Object.keys(bindings))
const declaredNodes = []
for (const name of [
  'view', 'books', 'characters', 'activeBook', 'activeCharacter',
  'chatInput', 'isLoading', 'pendingImages', 'currentMessages',
  'health', 'error', 'myName', 'uploading', 'editor',
  'openBook', 'openChat', 'openEditor', 'saveCharacter', 'removeBook',
  'removeCharacter', 'retryBook', 'handleUpload', 'handleFileSelect',
  'sendMessage', 'newChat', 'goShelf', 'backToGroup', 'statusText',
  'formatCount', 'renderMarkdown',
  'group', 'groupContainer', 'openGroup', 'loadGroupMembers', 'toggleMute',
  'memberStateText', 'groupMentionText', 'handleGroupKeydown', 'sendGroupMessage',
  'openMemberStatus', 'toggleMemberStatus', 'verifying', 'verifyBook',
  'merge', 'openMergePicker', 'confirmMerge', 'unmergeChild', 'mergeCandidates',
  'shelves', 'uploadShelfId', 'groupInfo', 'booksOfShelf', 'createShelf',
  'renameShelf', 'removeShelf', 'openShelfChat', 'enterGroup',
  'loadGroupHistory', 'turnToGroupMessage', 'openGroupInfo', 'closeGroupInfo',
  'visibleMembers', 'hasMoreMembers', 'saveGroupTitle', 'loadChatHistory',
  'MEMBER_COLS', 'MEMBER_ROWS', 'mergePanel', 'loadMergeCandidates', 'resolveMerge'
]) {
  if (!declared.has(name)) declaredNodes.push(name)
}
check('script 声明了模板用到的全部标识符', declaredNodes.length === 0, declaredNodes.join(', '))

// --- 三视图结构 ---
const template = descriptor.template.content
check('书架视图（书架卡片）',
  template.includes("view === 'shelf'") && template.includes('shelf-card'))
check('角色列表已并入群聊信息界面（群聊页不再有成员条）',
  !template.includes('group-members') && template.includes('member-grid'))
check('群聊信息是独立界面（跳转，不是弹窗）',
  template.includes("view === 'groupInfo'") && source.includes("view.value = 'groupInfo'"))
check('点成员打开角色详情', template.includes('openMemberStatus'))
check('聊天视图', template.includes('chat-page') && template.includes('chat-input-box'))
check('上传入口（txt/md/pdf）', template.includes('accept=".txt,.md,.markdown,.pdf"'))
check('角色编辑弹窗', template.includes('modal-mask') && template.includes('saveCharacter'))
check('抽取进度条', template.includes('book.progress'))
check('空书架引导', template.includes('还没有书架'))

// --- 旧产品残留必须清干净 ---
check('没有 API Key 输入框', !template.includes('type="password"') && !template.includes('API Key'))
check('没有分手恢复主题残留', !source.includes('分手') && !source.includes('心理治疗师'))
check('没有旧的 userSessions 状态', !source.includes('userSessions'))
check('没有旧的 mode 单选', !source.includes('selectedMode'))

// --- 关键调用 ---
check('调用 /api/books', source.includes("'/api/books'") || source.includes('`/api/books'))
check('调用 /api/session', source.includes("'/api/session'"))
check('调用 /api/chat', source.includes("'/api/chat'"))
check('按 character_id 发消息', source.includes('character_id: characterId'))
check('流式只做追加', source.includes('messages[index].content += event.text'))
check('Markdown 走净化模块', source.includes("from './utils/markdown.js'"))

// --- 群聊（一个书架 = 一个群）---
check('群聊：视图存在（成员条已移到信息界面）',
  template.includes("view === 'group'") && !template.includes('group-members'))
check('群聊：调用书架群聊接口', /\/api\/shelves\/\$\{[^}]+\}\/chat/.test(source))
check('群聊：读取群成员', /\/api\/shelves\/\$\{[^}]+\}\/members/.test(source))
check('群聊：按发言人分发流事件', source.includes("event.type === 'speaker_start'"))
check('群聊：展示发言人名字', template.includes('message-speaker'))
check('群聊：可切换在线/离线', source.includes('status: next'))
check('群聊：点书即进群', source.includes('await enterGroup('))
const headIndex = template.indexOf('class="chat-head"')
const headBlock = headIndex >= 0 ? template.slice(headIndex, headIndex + 900) : ''
check('群聊：顶部只剩「信息」按钮（其余迁入信息面板）',
  headBlock.includes('openGroupInfo') && !headBlock.includes('verifyBook'),
  headBlock.slice(0, 120).replace(/\s+/g, ' '))
check('群聊：书架页有群聊入口', template.includes('openShelfChat'))
check('群聊：信息面板显示成员数', template.includes('群聊信息（'))

// --- 群聊信息面板 ---
check('信息面板：成员网格 5 列', source.includes('MEMBER_COLS = 5') && template.includes('member-grid'))
check('信息面板：默认 3 行', source.includes('MEMBER_ROWS = 3'))
check('信息面板：超过 3 行出现「更多」', template.includes('hasMoreMembers') && template.includes('更多'))
check('信息面板：点成员跳详情', template.includes('openMemberStatus(member)'))
check('信息面板：可改群聊名', template.includes('saveGroupTitle') && source.includes('groupInfo.title'))

// --- 书架管理 ---
check('书架：新建', template.includes('createShelf'))
check('书架：改名', template.includes('renameShelf'))
check('书架：删除（级联删书）', template.includes('removeShelf'))
check('书架：上传可选目标书架', source.includes("form.append('shelf_id'"))
check('书架：不再有「选择角色」按钮', !template.includes('选择角色'))

// --- 聊天历史 ---
check('历史：进入群聊加载后端记录', source.includes('loadGroupHistory') && /shelves\/\$\{[^}]+\}\/history/.test(source))
check('历史：进入单聊加载后端记录', source.includes('loadChatHistory') && /characters\/\$\{[^}]+\}\/history/.test(source))
check('历史：流水转消息（区分用户/角色）', source.includes('turnToGroupMessage'))
check('合并候选：信息页有「待确认的合并」入口',
  template.includes('mergePanel.pairs') && template.includes('merge-row'))
check('合并候选：只读拉取候选',
  source.includes('/merge/candidates') && source.includes('loadMergeCandidates'))
check('合并候选：可判定「是/不是同一人」',
  source.includes('/merge/resolve') && template.includes('resolveMerge(pair, true)')
  && template.includes('resolveMerge(pair, false)'))
check('默认落地：打开应用直接进群聊',
  source.includes('LAST_SHELF_KEY') && source.includes('await openShelfChat(target)'))
check('清空对话：群聊信息里有入口', template.includes('clearGroupHistory'))
check('清空对话：调 DELETE 历史接口', /method: 'DELETE'/.test(source))
check('清空对话：新对话会同步清后端', source.includes('tokenStorageKey(character.id)') && source.includes('async function newChat'))

// --- 手动合并 ---
check('合并：详情面板可手动合并', template.includes('openMergePicker') && template.includes('merge-item'))
check('合并：可撤销合并', template.includes('unmergeChild'))
check('合并：编辑弹窗可填别名', template.includes('aliasesText'))
check('合并：调用合并接口', /\/api\/characters\/\$\{[^}]+\}\/merge/.test(source))
check('合并：调用撤销接口', /\/api\/characters\/\$\{[^}]+\}\/unmerge/.test(source))

// --- 贴纸 ---
check('贴纸：渲染 Twemoji 图片', template.includes('class="sticker"') && template.includes('message.sticker.url'))
check('贴纸：单聊解析 sticker 事件', source.includes("event.type === 'sticker'"))
check('贴纸：群聊接收 speaker_end 的贴纸', source.includes('.sticker = event.sticker'))
check('贴纸：加载失败回退到系统 emoji', template.includes('stickerFailed'))

// --- RAG 记忆库 ---
check('记忆库：展示后端状态', template.includes('health?.rag'))
check('记忆库：区分向量/词法', source.includes("backend === 'vector'"))

console.log(failed ? `\n失败 ${failed} 项` : '\n全部通过')
process.exit(failed ? 1 : 0)
