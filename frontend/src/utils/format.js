/** 纯展示用的格式化函数：不碰状态、不请求接口，方便单测与复用。 */

/** 作品状态文案 */
export function statusText(book) {
  if (book.status === 'ready') return `✓ 已就绪 · ${book.character_count ?? 0} 个角色`
  if (book.status === 'failed') return `✗ ${book.error || '抽取失败'}`
  return `抽取中 ${book.progress || 0}% · ${book.stage || '排队中'}`
}

/** 字数：过万显示 x.x 万 */
export function formatCount(value) {
  const number = Number(value || 0)
  return number >= 10000 ? `${(number / 10000).toFixed(1)} 万` : `${number}`
}

/** 时间：今天只显示时分，更早显示日期 */
export function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  const pad = (n) => String(n).padStart(2, '0')
  if (sameDay) return `${pad(date.getHours())}:${pad(date.getMinutes())}`
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 头像走本地代理（避免盗链 / 混合内容） */
export function avatarSrc(url) {
  return url ? `/api/avatar?url=${encodeURIComponent(url)}` : ''
}

/** 群成员状态文案 */
export function memberStateText(member) {
  if (!member) return ''
  if (member.status === 'offline') return '离线'
  if (member.state === 'cooldown') return '刚说过'
  if (member.state === 'speaking') return '发言中'
  return '在线'
}

/** @ 列表文案 */
export function groupMentionText(mentions) {
  return (mentions || [])
    .map((item) => (item === 'all' ? '全体成员' : item))
    .join('、')
}