// 静态校验前端：SFC 能否编译 + 关键结构是否在位。
// 拆分后前端是多文件，所以这里扫描整个 src/，不再只看 App.vue。
const fs = require('fs')
const path = require('path')

const root = path.resolve(__dirname, '..')
const srcDir = path.join(root, 'frontend/src')
const { parse, compileScript, compileTemplate } = require(
  path.join(root, 'frontend/node_modules/@vue/compiler-sfc')
)

let failed = 0
const check = (name, ok, detail = '') => {
  if (!ok) failed += 1
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${ok || !detail ? '' : `  <- ${detail}`}`)
}

// ---------------------------------------------------------------- 收集源码
const walk = (dir) =>
  fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name)
    return entry.isDirectory() ? walk(full) : [full]
  })

const files = walk(srcDir).filter((file) => /\.(vue|js)$/.test(file))
const sources = {}
for (const file of files) sources[file] = fs.readFileSync(file, 'utf8')

const allSource = Object.values(sources).join('\n')
const vueFiles = files.filter((file) => file.endsWith('.vue'))
const allTemplate = vueFiles
  .map((file) => (sources[file].match(/<template>([\s\S]*)<\/template>/) || ['', ''])[1])
  .join('\n')
const css = fs.readFileSync(path.join(srcDir, 'styles/app.css'), 'utf8')

console.log(`扫描 ${files.length} 个文件（.vue ${vueFiles.length} 个）`)

// ---------------------------------------------------------------- 逐个 SFC 编译
const parseErrors = []
const templateErrors = []
const missingBindings = []

const BUILTINS = new Set([
  'true', 'false', 'null', 'undefined', 'Math', 'JSON', 'Number', 'String',
  'Object', 'Array', 'Date', 'console', 'window', 'document', 'localStorage',
  'event', '$event', 'index', 'length', 'value'
])

for (const file of vueFiles) {
  const source = sources[file]
  const { descriptor, errors } = parse(source, { filename: file })
  if (errors.length) parseErrors.push(`${path.basename(file)}: ${errors.map((e) => e.message).join('; ')}`)
  if (!descriptor.template) continue

  let bindings = {}
  try {
    bindings = compileScript(descriptor, { id: path.basename(file) }).bindings || {}
  } catch (error) {
    missingBindings.push(`${path.basename(file)}: script 编译失败 ${error.message}`)
  }

  const template = descriptor.template.content
  const compiled = compileTemplate({
    source: template,
    filename: file,
    id: path.basename(file),
    compilerOptions: { bindingMetadata: bindings }
  })
  if (compiled.errors.length) {
    templateErrors.push(`${path.basename(file)}: ${compiled.errors.map(String).join('; ')}`)
  }

  // --- 模板里用到的名字，必须能在 useApp() 里拿到 ---
  const destructured = new Set()
  const destructureMatch = source.match(/const\s*\{([\s\S]*?)\}\s*=\s*useApp\(\)/)
  if (destructureMatch) {
    for (const raw of destructureMatch[1].split(',')) {
      const name = raw.trim().split(':')[0].trim()
      if (name) destructured.add(name)
    }
  }
  // <script setup> 里自己声明的名字也合法（local computed / 处理函数）
  const locals = new Set()
  for (const match of source.matchAll(/\b(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)/g)) {
    locals.add(match[1])
  }

  const aliases = new Set()
  for (const match of template.matchAll(/v-for="\s*\(?([^)]*?)\)?\s+in\s/g)) {
    for (const raw of match[1].split(',')) aliases.add(raw.trim())
  }
  for (const match of template.matchAll(/v-slot:[^=]*="\{([^}]*)\}"/g)) {
    for (const raw of match[1].split(',')) aliases.add(raw.trim().split(':')[0].trim())
  }
  for (const match of template.matchAll(/#[\w-]+="\{([^}]*)\}"/g)) {
    for (const raw of match[1].split(',')) aliases.add(raw.trim().split(':')[0].trim())
  }

  const used = new Set()
  // 只取表达式本身：{{ ... }} 和 :attr="@attr" 的值，不含属性名
  const regions = [
    ...[...template.matchAll(/\{\{([\s\S]*?)\}\}/g)].map((m) => m[1]),
    ...[...template.matchAll(/(?:^|\s)(?::|@|v-|#)[\w:.-]+="([^"]*)"/g)].map((m) => m[1])
  ]
  for (const raw of regions) {
    const cleaned = raw
      .replace(/'[^']*'|"[^"]*"|`[^`]*`/g, ' ')      // 字符串字面量
      .replace(/\.\s*[A-Za-z_$][\w$]*/g, '')       // 属性访问：只留根对象
      .replace(/[{,]\s*[A-Za-z_$][\w$]*\s*:/g, ' ') // 对象字面量的键
    for (const match of cleaned.matchAll(/\b([A-Za-z_$][\w$]*)\b/g)) used.add(match[1])
  }

  const missing = []
  for (const name of used) {
    if (destructured.has(name) || aliases.has(name) || locals.has(name)
      || BUILTINS.has(name)) continue
    if (/^[A-Z]/.test(name)) continue // 组件
    if (['in', 'of', 'typeof', 'new', 'return', 'if', 'else'].includes(name)) continue
    missing.push(name)
  }
  if (missing.length) {
    missingBindings.push(`${path.basename(file)}: ${[...new Set(missing)].join(', ')}`)
  }
}

check('全部 SFC 解析通过', parseErrors.length === 0, parseErrors.join(' | '))
check('全部 template 编译通过', templateErrors.length === 0, templateErrors.join(' | '))
check('模板用到的标识符都能从 store 拿到', missingBindings.length === 0, missingBindings.join(' | '))
check('存在全局样式', css.length > 500, `${css.length} 字节`)

// ---------------------------------------------------------------- 结构
const appVue = sources[path.join(srcDir, 'App.vue')] || ''
check('App.vue 只剩外壳（< 120 行）', appVue.split('\n').length < 120, `${appVue.split('\n').length} 行`)
check('页面拆到 pages/', ['ShelfPage', 'GroupChatPage', 'GroupInfoPage', 'CharacterChatPage']
  .every((name) => fs.existsSync(path.join(srcDir, 'pages', `${name}.vue`))))
check('浮层拆到 components/modals/',
  ['MergePicker', 'CharacterEditor', 'CharacterStatus']
    .every((name) => fs.existsSync(path.join(srcDir, 'components/modals', `${name}.vue`))))
check('状态收进 stores/app.js', fs.existsSync(path.join(srcDir, 'stores/app.js')))
check('请求封装独立在 api/client.js', fs.existsSync(path.join(srcDir, 'api/client.js')))

// ---------------------------------------------------------------- store 绑定完整性
// 拆成多组件后最容易踩的坑：页面解构了 store 里并不存在的名字，
// 浏览器里不会报错，只会静默变成 undefined —— 这里静态揪出来。
const storeFile = path.join(srcDir, 'stores/app.js')
const storeSource = fs.existsSync(storeFile) ? fs.readFileSync(storeFile, 'utf8') : ''
const storeDeclared = new Set()
for (const m of storeSource.matchAll(/^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[=:]/gm)) storeDeclared.add(m[1])
for (const m of storeSource.matchAll(/^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/gm)) storeDeclared.add(m[1])
for (const m of storeSource.matchAll(/^import\s+([\s\S]*?)\s+from\s+['"][^'"]+['"]/gm)) {
  for (const part of m[1].replace(/[{}]/g, ',').split(',')) {
    const name = part.trim().split(/\s+as\s+/).pop().trim()
    if (name && name !== '*') storeDeclared.add(name)
  }
}
const stateKeys = (storeSource.match(/const state = \{([\s\S]*?)\n\}/) || ['', ''])[1]
  .split(/,\s*\n/).map((line) => line.trim().replace(/,$/, '')).filter(Boolean)
const ghostExports = stateKeys.filter((key) => !storeDeclared.has(key))
check('store 导出的每个键都有定义', ghostExports.length === 0, ghostExports.join(', '))

const badDestructure = []
for (const vueFile of vueFiles) {
  const text = sources[vueFile]
  const match = text.match(/const\s*\{([\s\S]*?)\}\s*=\s*useApp\(\)/)
  if (!match) continue
  const names = match[1].split(',').map((s) => s.trim()).filter(Boolean)
  const missing = names.filter((name) => !stateKeys.includes(name))
  if (missing.length) badDestructure.push(`${path.basename(vueFile)}: ${missing.join(', ')}`)
}
check('组件解构的名字都存在于 store', badDestructure.length === 0, badDestructure.join(' | '))

// ---------------------------------------------------------------- 视图结构
check('书架视图（书架卡片）', allTemplate.includes('shelf-card'))
check('角色列表已并入群聊信息界面（群聊页不再有成员条）',
  !allTemplate.includes('group-members') && allTemplate.includes('member-grid'))
check('群聊信息是独立界面（跳转，不是弹窗）',
  allTemplate.includes('member-grid') && allSource.includes("view.value = 'groupInfo'"))
check('点成员打开角色详情', allTemplate.includes('openMemberStatus'))
check('聊天视图', allTemplate.includes('chat-page') && allTemplate.includes('chat-input-box'))
check('上传入口（txt/md/pdf）', allTemplate.includes('accept=".txt,.md,.markdown,.pdf"'))
check('角色编辑弹窗', allTemplate.includes('modal-mask') && allTemplate.includes('saveCharacter'))
check('抽取进度条', allTemplate.includes('book.progress'))
check('空书架引导', allTemplate.includes('还没有书架'))

// ---------------------------------------------------------------- 旧产品残留
check('没有 API Key 输入框', !allTemplate.includes('type="password"') && !allTemplate.includes('API Key'))
check('没有分手恢复主题残留', !allSource.includes('分手') && !allSource.includes('心理治疗师'))
check('没有旧的 userSessions 状态', !allSource.includes('userSessions'))
check('没有旧的 mode 单选', !allSource.includes('selectedMode'))

// ---------------------------------------------------------------- 关键调用
check('调用 /api/books', allSource.includes("'/api/books'") || allSource.includes('`/api/books'))
check('调用 /api/session', allSource.includes("'/api/session'"))
check('调用 /api/chat', allSource.includes("'/api/chat'"))
check('按 character_id 发消息', allSource.includes('character_id: characterId'))
check('流式只做追加', allSource.includes('messages[index].content += event.text'))
check('Markdown 走净化模块', allSource.includes('utils/markdown.js'))

// ---------------------------------------------------------------- 群聊
check('群聊：视图存在（成员条已移到信息界面）',
  allTemplate.includes('message-speaker') && !allTemplate.includes('group-members'))
check('群聊：调用书架群聊接口', /\/api\/shelves\/\$\{[^}]+\}\/chat/.test(allSource))
check('群聊：读取群成员', /\/api\/shelves\/\$\{[^}]+\}\/members/.test(allSource))
check('群聊：按发言人分发流事件', allSource.includes("event.type === 'speaker_start'"))
check('群聊：展示发言人名字', allTemplate.includes('message-speaker'))
check('群聊：可切换在线/离线', allSource.includes('status: next'))
check('群聊：点书即进群', allSource.includes('await enterGroup('))
const groupPage = sources[path.join(srcDir, 'pages/GroupChatPage.vue')] || ''
const headIndex = groupPage.indexOf('class="chat-head"')
const headBlock = headIndex >= 0 ? groupPage.slice(headIndex, headIndex + 1600) : ''
check('群聊：顶部只剩「信息」按钮（其余迁入信息面板）',
  headBlock.includes('openGroupInfo') && !headBlock.includes('verifyBook'),
  headBlock.slice(0, 120).replace(/\s+/g, ' '))
check('群聊：书架页有群聊入口', allTemplate.includes('openShelfChat'))
check('群聊：信息面板显示成员数', allTemplate.includes('群聊信息（'))

// ---------------------------------------------------------------- 信息面板
check('信息面板：成员网格 5 列', allSource.includes('MEMBER_COLS = 5') && allTemplate.includes('member-grid'))
check('信息面板：默认 3 行', allSource.includes('MEMBER_ROWS = 3'))
check('信息面板：超过 3 行出现「更多」', allTemplate.includes('hasMoreMembers') && allTemplate.includes('更多'))
check('信息面板：点成员跳详情', allTemplate.includes('openMemberStatus(member)'))
check('信息面板：可改群聊名', allTemplate.includes('saveGroupTitle') && allSource.includes('groupInfo.title'))

// ---------------------------------------------------------------- 书架
check('书架：新建', allTemplate.includes('createShelf'))
check('书架：改名', allTemplate.includes('renameShelf'))
check('书架：删除（级联删书）', allTemplate.includes('removeShelf'))
check('书架：上传可选目标书架', allSource.includes("form.append('shelf_id'"))
check('书架：不再有「选择角色」按钮', !allTemplate.includes('选择角色'))

// ---------------------------------------------------------------- 历史与合并
check('历史：进入群聊加载后端记录',
  allSource.includes('loadGroupHistory') && /shelves\/\$\{[^}]+\}\/history/.test(allSource))
check('历史：进入单聊加载后端记录',
  allSource.includes('loadChatHistory') && /characters\/\$\{[^}]+\}\/history/.test(allSource))
check('历史：流水转消息（区分用户/角色）', allSource.includes('turnToGroupMessage'))
check('合并候选：信息页有「待确认的合并」入口',
  allTemplate.includes('mergePanel.pairs') && allTemplate.includes('merge-row'))
check('合并候选：只读拉取候选',
  allSource.includes('/merge/candidates') && allSource.includes('loadMergeCandidates'))
check('合并候选：可判定「是/不是同一人」',
  allSource.includes('/merge/resolve') && allTemplate.includes('resolveMerge(pair, true)'))
check('默认落地：打开应用直接进群聊',
  allSource.includes('LAST_SHELF_KEY') && allSource.includes('await openShelfChat(target)'))
check('清空对话：群聊信息里有入口', allTemplate.includes('clearGroupHistory'))
check('清空对话：调 DELETE 历史接口', /method: 'DELETE'/.test(allSource))
check('清空对话：新对话会同步清后端',
  allSource.includes('tokenStorageKey(character.id)') && allSource.includes('async function newChat'))
check('合并：详情面板可手动合并', allTemplate.includes('openMergePicker') && allTemplate.includes('merge-item'))
check('合并：可撤销合并', allTemplate.includes('unmergeChild'))
check('合并：编辑弹窗可填别名', allTemplate.includes('aliasesText'))
check('合并：调用合并接口', /\/api\/characters\/\$\{[^}]+\}\/merge/.test(allSource))
check('合并：调用撤销接口', /\/api\/characters\/\$\{[^}]+\}\/unmerge/.test(allSource))

// ---------------------------------------------------------------- 贴纸
check('贴纸：渲染 Twemoji 图片', allTemplate.includes('message.sticker.url'))
check('贴纸：单聊解析 sticker 事件', allSource.includes("event.type === 'sticker'"))
check('贴纸：群聊接收 speaker_end 的贴纸', allSource.includes('event.sticker || null'))
check('贴纸：加载失败回退到系统 emoji', allSource.includes('onerror') || allSource.includes('@error'))

console.log(failed === 0 ? '\n全部通过' : `\n失败 ${failed} 项`)
process.exit(failed === 0 ? 0 : 1)