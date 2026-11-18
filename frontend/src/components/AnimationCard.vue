<template>
  <div class="anim-card">
    <div class="anim-head">
      <span class="anim-title">🎬 动画</span>
      <span class="anim-stage" :class="'stage-' + state">{{ stageLabel }}</span>
    </div>

    <p v-if="prompt" class="anim-prompt">{{ prompt }}</p>

    <video
      v-if="ready"
      class="anim-video"
      :src="videoUrl"
      controls
      preload="metadata"
      playsinline
    ></video>

    <div v-if="state === 'awaiting'" class="anim-actions">
      <button class="primary small" :disabled="confirming" @click="confirm">
        {{ confirming ? '提交中…' : '确认分镜，开始渲染' }}
      </button>
    </div>

    <div v-if="ready" class="anim-actions">
      <a class="ghost small" :href="videoUrl + '?download=1'">下载 mp4</a>
    </div>

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
  awaiting_confirmation: '等确认分镜',
  scaffold: '搭项目',
  render: '逐帧渲染',
  encode: '编码视频',
  done: '完成',
  failed: '失败',
}

const stage = ref('scaffold')
const state = ref('running')
const error = ref('')
const confirming = ref(false)

async function confirm() {
  confirming.value = true
  try {
    await fetch(`/api/animation/${props.jobId}/confirm`, { method: 'POST' })
    state.value = 'running'
    poll()
  } catch {
    error.value = '确认失败，试试右侧任务栏'
  } finally {
    confirming.value = false
  }
}

const ready = computed(() => state.value === 'done')
const stageLabel = computed(() => STAGE_TEXT[stage.value] || stage.value || '处理中')
const videoUrl = computed(() => `/api/animation/${props.jobId}/video`)

let timer = null

async function poll() {
  try {
    const response = await fetch(`/api/animation/${props.jobId}`)
    if (!response.ok) return
    const job = await response.json()
    stage.value = job.stage || stage.value
    if (job.stage === 'awaiting_confirmation') {
      state.value = 'awaiting'
    } else if (job.stage === 'done') {
      state.value = 'done'
      stop()
    } else if (job.stage === 'failed') {
      state.value = 'failed'
      error.value = job.error || ''
      stop()
    }
  } catch {
    // 轮询失败不打扰用户，下次再试
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
  timer = setInterval(poll, 5000)
})

onUnmounted(stop)
</script>