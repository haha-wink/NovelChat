/** 前端与后端的唯一通道。
 *
 * 页面里不要再裸写 `fetch('/api/...')`：
 * 统一走这里，错误信息（后端 `detail`）会被归一成 `Error.message`，
 * 调用方只需要 `try/catch` 后把 message 丢给界面。
 */

/**
 * 发一个 JSON 请求。
 * @param {string} path  以 /api 开头的路径
 * @param {{method?: string, body?: any, headers?: Record<string,string>}} options
 */
export async function api(path, { method = 'GET', body, headers } = {}) {
  const init = { method, headers: { ...(headers || {}) } }
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }

  const response = await fetch(path, init)
  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`
    try {
      const data = await response.json()
      if (data?.detail) detail = data.detail
    } catch (parseError) {
      /* 非 JSON 错误体，保留默认文案 */
    }
    const failure = new Error(detail)
    failure.status = response.status
    throw failure
  }

  if (response.status === 204) return null
  return response.json()
}

/** 上传文件（multipart），单独一个函数避免和 JSON 请求混在一起。 */
export async function upload(path, formData) {
  const response = await fetch(path, { method: 'POST', body: formData })
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
  return response.json()
}

/** 常用接口的一层薄封装。新代码优先用它，路径就不会散落各处。 */
export const apiPaths = {
  health: '/api/health',
  session: '/api/session',
  shelves: '/api/shelves',
  shelf: (id) => `/api/shelves/${id}`,
  shelfMembers: (id) => `/api/shelves/${id}/members`,
  shelfHistory: (id) => `/api/shelves/${id}/history`,
  shelfChat: (id) => `/api/shelves/${id}/chat`,
  shelfMemories: (id) => `/api/shelves/${id}/memories`,
  shelfMerge: (id) => `/api/shelves/${id}/merge`,
  shelfMergeCandidates: (id) => `/api/shelves/${id}/merge/candidates`,
  shelfMergeResolve: (id) => `/api/shelves/${id}/merge/resolve`,
  books: '/api/books',
  book: (id) => `/api/books/${id}`,
  bookCharacters: (id) => `/api/books/${id}/characters`,
  character: (id) => `/api/characters/${id}`,
  characterStatus: (id) => `/api/characters/${id}/status`,
  characterHistory: (id) => `/api/characters/${id}/history`,
  characterAvatars: (id) => `/api/characters/${id}/avatars`,
  characterMerge: (id) => `/api/characters/${id}/merge`,
  characterUnmerge: (id) => `/api/characters/${id}/unmerge`,
  chat: '/api/chat',
  ragStatus: '/api/rag/status'
}