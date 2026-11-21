// check-compaction.mjs — 读 DSH 会话日志（多 zstd 帧追加格式），统计压缩行为
// 用法: node check-compaction.mjs <session.v4.jsonl.zstd> [more files...]
import { readFileSync } from 'node:fs'
import { zstdDecompressSync } from 'node:zlib'

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

function readEvents(file) {
  const buf = readFileSync(file)
  const offs = []
  let i = buf.indexOf(MAGIC)
  while (i !== -1) { offs.push(i); i = buf.indexOf(MAGIC, i + 4) }
  const parts = []
  for (let k = 0; k < offs.length; k++) {
    try {
      parts.push(zstdDecompressSync(buf.subarray(offs[k], k + 1 < offs.length ? offs[k + 1] : buf.length)))
    } catch { /* 跳过坏帧 */ }
  }
  const events = []
  for (const line of Buffer.concat(parts).toString('utf8').split('\n')) {
    const t = line.trim()
    if (!t.startsWith('{')) continue
    try { events.push(JSON.parse(t)) } catch { /* 跳过 */ }
  }
  return { size: buf.length, events }
}

function summarize(file) {
  const { size, events } = readEvents(file)
  let compactions = 0, prunes = 0, prunedTokens = 0, toolResults = 0, turns = 0
  let lastCompactionTurn = null, lastCompactionTime = null
  const summaryChars = []
  for (const ev of events) {
    switch (ev.type) {
      case 'compaction/start':
        compactions++
        lastCompactionTurn = ev.data?.turn ?? null
        lastCompactionTime = ev.time ?? null
        break
      case 'compaction/prune':
        prunes++
        prunedTokens += ev.data?.shadowedTokenCount ?? 0
        break
      case 'compaction/summary':
        summaryChars.push(JSON.stringify(ev.data?.summary ?? '').length)
        break
      case 'tool/result': toolResults++; break
      case 'turn/start': turns++; break
    }
  }
  const last = lastCompactionTime ? new Date(lastCompactionTime) : null
  const pad = (n) => String(n).padStart(4)
  const parts = [
    `压缩 ${pad(compactions)} 次`,
    `prune ${pad(prunes)} 次(${Math.round(prunedTokens / 1000)}k tok)`,
    `工具结果 ${String(toolResults).padStart(4)}`,
    `${String(turns).padStart(3)} 轮`,
  ]
  if (last) parts.push(`末次压缩 第${lastCompactionTurn}轮 ${last.toISOString().slice(5, 16).replace('T', ' ')}`)
  if (summaryChars.length) {
    const avg = Math.round(summaryChars.reduce((a, b) => a + b, 0) / summaryChars.length)
    parts.push(`摘要均长 ${Math.round(avg / 1000)}k 字符`)
  }
  parts.push(`${(size / 1048576).toFixed(1)} MB`)
  return parts.join(' · ')
}

for (const f of process.argv.slice(2)) {
  try { console.log(`${summarize(f)}  ${f}`) } catch (e) { console.log(`读取失败 ${f}: ${e.message}`) }
}