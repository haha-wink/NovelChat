/** SSE 解析：把 fetch 的流拆成一条条事件。
 *
 * 后端（channels/web.py）发的是 `data: {json}\n\n`，这里只负责拆帧和反序列化，
 * 具体怎么处理事件交给调用方——单聊和群聊共用同一套解析，避免两处抄一遍。
 */

/**
 * @param {Response} response  fetch 的原始响应（必须是流式）
 * @param {(event: any) => void} onEvent  每解析出一个事件就回调一次
 */
export async function readSSEStream(response, onEvent) {
  if (!response.body) return

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() // 最后一段可能不完整，留到下一轮

    for (const part of parts) {
      const line = part.trim()
      if (!line.startsWith('data:')) continue

      const payload = line.slice(5).trim()
      if (!payload || payload === '[DONE]') continue

      try {
        onEvent(JSON.parse(payload))
      } catch (parseError) {
        console.error('解析流数据失败', parseError, payload)
      }
    }
  }
}