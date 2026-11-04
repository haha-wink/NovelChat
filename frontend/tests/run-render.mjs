// SSR 运行器：只补 localStorage，不要伪造 document（runtime-dom 靠它是否为 undefined 判断环境）
const store = new Map()
globalThis.localStorage = {
  getItem: (k) => (store.has('' + k) ? store.get('' + k) : null),
  setItem: (k, v) => store.set('' + k, '' + v),
  removeItem: (k) => store.delete('' + k)
}

const { renderAll } = await import('../.ssr-check/render-smoke.js')
const out = await renderAll()
for (const [view, html] of Object.entries(out)) {
  const markers = (html.match(/class="[a-z-]+"/g) || []).length
  console.log(`  ${view.padEnd(10)} ${String(html.length).padStart(6)} 字符 / ${markers} 个 class`)
  if (html.length < 100) throw new Error(`${view} 渲染结果异常`)
}
console.log('SSR 四视图 + 浮层渲染全部通过')

