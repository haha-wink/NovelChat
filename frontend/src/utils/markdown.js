import { marked } from 'marked'

// `breaks: true` 让单个换行按换行渲染（原来是把所有 \n 硬替换成 <br>，
// 那会破坏代码块、列表、表格的 Markdown 结构）
marked.setOptions({ gfm: true, breaks: true })

//: 连内容一起丢掉
const DROP_WITH_CONTENT = new Set([
  'script', 'style', 'iframe', 'frame', 'frameset', 'object', 'embed',
  'link', 'meta', 'base', 'form', 'input', 'button', 'textarea', 'select',
  'option', 'svg', 'math', 'template', 'noscript', 'canvas', 'video',
  'audio', 'source', 'track', 'area', 'map'
])

//: 允许保留的标签（Markdown 常见输出）
const ALLOWED_TAGS = new Set([
  'a', 'p', 'br', 'hr', 'strong', 'b', 'em', 'i', 'u', 's', 'del', 'ins',
  'mark', 'small', 'sub', 'sup', 'code', 'pre', 'blockquote', 'ul', 'ol',
  'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody',
  'tfoot', 'tr', 'th', 'td', 'caption', 'img', 'span', 'div', 'figure',
  'figcaption', 'dl', 'dt', 'dd', 'abbr', 'kbd', 'samp', 'var'
])

const TAG_ATTRS = {
  a: ['href', 'title'],
  img: ['src', 'alt', 'title', 'width', 'height'],
  th: ['colspan', 'rowspan', 'align'],
  td: ['colspan', 'rowspan', 'align'],
  ol: ['start', 'type'],
  li: ['value'],
  code: ['class'],
  pre: ['class'],
  span: ['class'],
  div: ['class'],
  p: ['class'],
  table: ['class'],
  blockquote: ['class'],
  abbr: ['title']
}

const URL_ATTRS = new Set(['href', 'src'])

const NODE_ELEMENT = 1
const NODE_COMMENT = 8

function isSafeUrl(raw, { allowDataImage = false } = {}) {
  // 去掉空白/控制字符，防止 `java\nscript:` 这类绕过
  const compact = String(raw || '').replace(/[\u0000-\u0020\u00a0\u2000-\u200f]/g, '')
  if (!compact) return false

  const lower = compact.toLowerCase()
  if (lower.startsWith('data:')) {
    return allowDataImage && /^data:image\/(png|jpe?g|gif|webp);base64,/i.test(compact)
  }

  const schemeMatch = lower.match(/^([a-z][a-z0-9+.-]*):/i)
  if (!schemeMatch) return true // 相对路径 / 锚点
  return ['http', 'https', 'mailto', 'tel'].includes(schemeMatch[1])
}

function cleanAttributes(el, tag) {
  const allowed = TAG_ATTRS[tag] || []

  for (const attr of Array.from(el.attributes)) {
    const name = attr.name.toLowerCase()

    // 事件处理器、样式、可执行属性一律删掉
    if (name.startsWith('on') || ['style', 'srcdoc', 'formaction', 'xlink:href', 'is'].includes(name)) {
      el.removeAttribute(attr.name)
      continue
    }
    if (!allowed.includes(name)) {
      el.removeAttribute(attr.name)
      continue
    }
    if (URL_ATTRS.has(name) && !isSafeUrl(attr.value, { allowDataImage: tag === 'img' })) {
      el.removeAttribute(attr.name)
    }
  }

  if (tag === 'a' && el.hasAttribute('href')) {
    el.setAttribute('target', '_blank')
    el.setAttribute('rel', 'noopener noreferrer nofollow')
  }
  if (tag === 'img') {
    el.setAttribute('loading', 'lazy')
    el.setAttribute('referrerpolicy', 'no-referrer')
  }
}

function unwrap(el) {
  const parent = el.parentNode
  if (!parent) return
  while (el.firstChild) parent.insertBefore(el.firstChild, el)
  parent.removeChild(el)
}

function sanitizeChildren(node) {
  for (const child of Array.from(node.childNodes)) {
    if (child.nodeType === NODE_COMMENT) {
      child.remove()
      continue
    }
    if (child.nodeType !== NODE_ELEMENT) continue

    const tag = child.tagName.toLowerCase()
    if (DROP_WITH_CONTENT.has(tag)) {
      child.remove()
      continue
    }

    sanitizeChildren(child)

    if (!ALLOWED_TAGS.has(tag)) {
      // 未知标签去掉壳、保留文字内容
      unwrap(child)
      continue
    }
    cleanAttributes(child, tag)
  }
}

export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ))
}

/**
 * 白名单净化：只保留安全的标签/属性，移除脚本、事件处理器与危险 URL。
 * 没有 DOM 的环境（SSR / 单测）降级为纯文本转义。
 */
export function sanitizeHtml(html) {
  const source = String(html ?? '')
  if (!source) return ''
  if (typeof DOMParser === 'undefined') return escapeHtml(source)

  const doc = new DOMParser().parseFromString(
    `<!DOCTYPE html><body><div id="sanitize-root">${source}</div></body>`,
    'text/html'
  )
  const root = doc.getElementById('sanitize-root')
  if (!root) return escapeHtml(source)

  sanitizeChildren(root)
  return root.innerHTML
}

/** Markdown -> 已净化的 HTML */
export function renderMarkdown(text) {
  if (!text) return ''
  try {
    return sanitizeHtml(marked.parse(String(text)))
  } catch (error) {
    console.error('Markdown 渲染失败，降级为纯文本', error)
    return escapeHtml(String(text))
  }
}
