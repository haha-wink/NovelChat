// 群聊事件流的回归测试：验证「被查重折叠的消息真的不会显示」。
//
// 这一条踩过两次坑：
//   1) active 存的是数组下标，speaker_end 删除后 speaker_drop 找不到人
//   2) speaker_drop 比 speaker_end 晚到（聚合节点在所有人生成完之后才跑）
//
// 跑法：node frontend/tests/group-stream.test.mjs

globalThis.fetch = async () => { throw new Error('测试里不该发请求') }

// store 依赖 localStorage（会话令牌），这里补一个内存实现
const store = new Map()
globalThis.localStorage = {
  getItem: (key) => (store.has('' + key) ? store.get('' + key) : null),
  setItem: (key, value) => store.set('' + key, '' + value),
  removeItem: (key) => store.delete('' + key)
}

const { useApp } = await import('../src/stores/app.js')
const app = useApp()

/** 把事件数组包成 fetch 的流式响应 */
function streamOf(items) {
  const text = items.map((item) => `data: ${JSON.stringify(item)}\n\n`).join('')
  const bytes = new TextEncoder().encode(text)
  return {
    body: {
      getReader() {
        let sent = false
        return {
          async read() {
            if (sent) return { done: true, value: undefined }
            sent = true
            return { done: false, value: bytes }
          }
        }
      }
    }
  }
}

const failures = []
function check(name, ok, detail = '') {
  if (ok) {
    console.log(`  ✓ ${name}`)
    return
  }
  failures.push(name)
  console.log(`  ✗ ${name}  ${detail}`)
}

// ---- 造两条发言：乙和甲内容重复，甲会被聚合节点折叠 ----
const items = [
  { type: 'user', text: '帮我排个先后' },
  { type: 'round_start', round: 1 },
  { type: 'speaker_start', character_id: 'c_yi', name: '乙', avatar: '🍁', angle: '方案' },
  { type: 'speaker_start', character_id: 'c_jia', name: '甲', avatar: '🌸', angle: '共情' },
  { type: 'speaker_end', character_id: 'c_yi', name: '乙', text: '先吃饭，别的明天再说。', mentions: [] },
  { type: 'speaker_end', character_id: 'c_jia', name: '甲', text: '先吃饭吧，其余明天。', mentions: [] },
  // 聚合节点判定甲重复，这个事件在 speaker_end 之后才来
  { type: 'speaker_drop', character_id: 'c_jia', name: '甲', similar_to: '乙' },
  { type: 'done', rounds: 1, speakers: ['乙', '甲'] }
]

app.group.messages.length = 0
await app.readGroupStream(streamOf(items))

const visible = app.visibleGroupMessages.value
check('折叠：重复的那条被隐藏', visible.every((m) => m.name !== '甲'),
  JSON.stringify(visible.map((m) => m.name)))
check('折叠：保留的那条还在', visible.some((m) => m.name === '乙'),
  JSON.stringify(visible.map((m) => m.name)))
check('折叠：原始数组里仍留着（可回溯）', app.group.messages.some((m) => m.name === '甲'))
check('折叠：乙的内容没被改坏',
  visible.find((m) => m.name === '乙')?.content === '先吃饭，别的明天再说。')

// ---- 没有 speaker_drop 时，两条都该显示 ----
const items2 = [
  { type: 'speaker_start', character_id: 'c_a', name: '甲', angle: '共情' },
  { type: 'speaker_start', character_id: 'c_b', name: '乙', angle: '方案' },
  { type: 'speaker_end', character_id: 'c_a', name: '甲', text: '我懂。', mentions: [] },
  { type: 'speaker_end', character_id: 'c_b', name: '乙', text: '先吃饭。', mentions: [] },
  { type: 'done', rounds: 1, speakers: ['甲', '乙'] }
]
app.group.messages.length = 0
await app.readGroupStream(streamOf(items2))
check('正常：两条都显示', app.visibleGroupMessages.value.length === 2,
  String(app.visibleGroupMessages.value.length))

// ---- 整段发送：即使没有 delta，speaker_end 也要把文本填上 ----
const items3 = [
  { type: 'speaker_start', character_id: 'c_c', name: '丙' },
  { type: 'speaker_end', character_id: 'c_c', name: '丙', text: '整段文本。', mentions: [] },
  { type: 'done', rounds: 1, speakers: ['丙'] }
]
app.group.messages.length = 0
await app.readGroupStream(streamOf(items3))
check('整段：speaker_end 填上了全文',
  app.visibleGroupMessages.value[0]?.content === '整段文本。',
  String(app.visibleGroupMessages.value[0]?.content))
check('整段：typing 关掉了', app.visibleGroupMessages.value[0]?.typing === false)

// ---- 会话栏分界线：宽度计算与重置 ----
globalThis.window = {
  innerWidth: 1906,
  addEventListener() {},
  removeEventListener() {}
}

check("分界线：自动宽度 = (1906-860)/2", app.currentSidebarWidth() === 523,
  String(app.currentSidebarWidth()))

globalThis.window.innerWidth = 900
check("分界线：窄屏夹到最小值 200", app.currentSidebarWidth() === 200,
  String(app.currentSidebarWidth()))

globalThis.window.innerWidth = 4000
check("分界线：超宽屏夹到最大值 760", app.currentSidebarWidth() === 760,
  String(app.currentSidebarWidth()))

app.group.sidebarWidth = 320
check("分界线：拖过之后用拖出来的宽度", app.currentSidebarWidth() === 320,
  String(app.currentSidebarWidth()))
app.group.sidebarHover = true
check("分界线：悬停不改变宽度（防抖动）",
  app.sidebarWidthPx() === '320px', app.sidebarWidthPx())
app.group.sidebarHover = false

app.resetSidebarWidth()
check("分界线：双击恢复自动宽度", app.group.sidebarWidth === 0,
  String(app.group.sidebarWidth))
check("分界线：恢复后不写死宽度", app.sidebarWidthPx() === '', app.sidebarWidthPx())

if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：${failures.join('、')}`)
  process.exit(1)
}
console.log('\n群聊事件流测试全部通过')