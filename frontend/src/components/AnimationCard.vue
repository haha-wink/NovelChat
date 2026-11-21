<template>
  <div class="anim-card">
    <div class="anim-head">
      <span class="anim-title">🎬 动画</span>
      <span class="anim-stage" :class="'stage-' + state">
        <span v-if="running" class="anim-spin"></span>
        {{ stageLabel }}
      </span>
    </div>

    <p v-if="prompt" class="anim-prompt">{{ prompt }}</p>

    <!-- 进行中：给出能判断"还在动"的证据（阶段 + 已用时 + 任务号） -->
    <div v-if="running" class="anim-progress">
      <div class="anim-bar">
        <span v-if="progress > 0" class="anim-bar-fill" :style="{ width: Math.round(progress * 100) + '%' }"></span>
        <span v-else></span>
      </div>
      <p class="anim-hint">
        <template v-if="progress > 0">渲染 {{ Math.round(progress * 100) }}% · </template>
        已跑 {{ elapsedText }} · {{ jobId }}
      </p>
    </div>

    <div v-if="state === 'awaiting'" class="anim-actions">
      <button class="primary small" :disabled="confirming" @click="confirm">
        {{ confirming ? '提交中…' : '确认分镜，开始渲染' }}
      </button>
    </div>

    <video
      v-if="ready"
      class="anim-video"
      :src="videoUrl"
      controls
      preload="metadata"
      playsinline
    ></video>

    <div v-if="ready" class="anim-actions">
      <a class="ghost small" :href="videoUrl + '?download=1'">下载 mp4</a>
    </div>

    <div v-if="state === 'stale' || state === 'failed'" class="anim-actions">
      <button class="ghost small danger" :disabled="deleting" @click="remove">
        {{ deleting ? '清理中…' : '删除这个任务' }}
      </button>
    </div>

    <p v-if="state === 'stale'" class="anim-error">
      这个任务是后端重启前留下的，已经停了——删掉重新发一次就行。
    </p>
    <p v-else-if="state === 'failed'" class="anim-error">
      没做成：{{ error || '渲染失败' }}
    </p>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'

const props = defineProps({
  jobId: { type: String, required: true },
  prompt: { type: String, default: '' },
})

const STAGE_TEXT = {
  queued: '排队中',
  awaiting_confirmation: '等确认分镜',
  scaffold: '搭项目',
  storyboard: '写分镜',
  render: '逐帧渲染',
  encode: '编码视频',
  done: '完成',
  failed: '失败',
}

const stage = ref('queued')
const state = ref('running')
const error = ref('')
const stale = ref(false)
const progress = ref(0)
const confirming = ref(false)
const deleting = ref(false)
const startedAt = ref(Date.now())
const now = ref(Date.now())

const running = computed(() => state.value === 'running' || state.value === 'awaiting')
const ready = computed(() => state.value === 'done')
const stageLabel = computed(() => {
  if (state.value === 'stale') return '已中断'
  return STAGE_TEXT[stage.value] || stage.value || '处理中'
})
const videoUrl = computed(() => `/api/animation/${props.jobId}/video`)
const elapsedText = computed(() => {
  const seconds = Math.max(0, Math.round((now.value - startedAt.value) / 1000))
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
})

let timer = null
let clock = null

async function poll() {
  try {
    const response = await fetch(`/api/animation/${props.jobId}`)
    if (!response.ok) return
    const job = await response.json()

    if (job.created_at) {
      const parsed = Date.parse(job.created_at)
      if (!Number.isNaN(parsed)) startedAt.value = parsed
    }
    stage.value = job.stage || stage.value
    stale.value = Boolean(job.stale)
    if (typeof job.progress === 'number') progress.value = job.progress

    if (job.stage === 'awaiting_confirmation') {
      state.value = 'awaiting'
    } else if (job.stage === 'done') {
      state.value = 'done'
      stop()
    } else if (job.stage === 'failed') {
      state.value = 'failed'
      error.value = job.error || ''
      stop()
    } else if (job.stale) {
      // 后端重启过：状态写着"渲染中"，但进程里早没这个任务了
      state.value = 'stale'
      stop()
    } else {
      state.value = 'running'
    }
  } catch {
    // 轮询失败不打扰用户，下一次再试
  }
}

async function confirm() {
  confirming.value = true
  try {
    await fetch(`/api/animation/${props.jobId}/confirm`, { method: 'POST' })
    state.value = 'running'
    stale.value = false
    startedAt.value = Date.now()
    poll()
  } catch {
    error.value = '确认失败，试试右侧任务栏'
  } finally {
    confirming.value = false
  }
}

async function remove() {
  deleting.value = true
  try {
    await fetch(`/api/animation/${props.jobId}`, { method: 'DELETE' })
    state.value = 'done'
  } catch {
    error.value = '删除失败，去任务栏重试'
  } finally {
    deleting.value = false
  }
}

function stop() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

onMounted(() => {
  poll()
  timer = setInterval(poll, 3000)
  clock = setInterval(() => { now.value = Date.now() }, 1000)
})

onUnmounted(() => {
  stop()
  if (clock) clearInterval(clock)
})
</script>