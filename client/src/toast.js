/**
 * 轻量全局 toast。
 *
 * 原因：旧代码里成功/失败反馈一半用 window.alert()（阻塞、样式无法统一），
 * 另一半用 this.$emit('toast', ...) —— 但父组件从来没监听过这个事件，
 * 于是 ProxyPool / Analytics 里的报错被静默吞掉，用户完全看不到失败原因。
 * 这里提供一个共享的响应式队列，由 App.vue 统一渲染。
 */
import { reactive } from 'vue'

let seq = 0

export const toastState = reactive({
  items: [],
})

function push(message, type = 'info', duration = 3600) {
  const text = message instanceof Error ? message.message : String(message ?? '')
  if (!text) return null
  const id = ++seq
  toastState.items.push({ id, text, type })
  if (duration > 0) {
    setTimeout(() => dismiss(id), duration)
  }
  return id
}

export function dismiss(id) {
  const i = toastState.items.findIndex((t) => t.id === id)
  if (i !== -1) toastState.items.splice(i, 1)
}

const toast = {
  success: (m, d) => push(m, 'success', d),
  error: (m, d) => push(m, 'error', d ?? 6000),
  warning: (m, d) => push(m, 'warning', d ?? 5000),
  info: (m, d) => push(m, 'info', d),
  show: push,
  dismiss,
}

export default toast
