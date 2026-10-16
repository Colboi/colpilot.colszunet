<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import './style.css'

const props = defineProps({
  plugin: {
    type: Object,
    required: true,
  },
  requestBackend: {
    type: Function,
    required: true,
  },
})

const campusStatus = ref(null)
const campusStatusError = ref('')
const campusUsernameInput = ref('')
const campusPasswordInput = ref('')
const campusCredentialsDirty = ref(false)
const campusCredentialsSaving = ref(false)
const campusCredentialsMessage = ref('')
const campusCredentialsMessageTone = ref('success')
const campusIntervalInput = ref(300)
const campusIntervalDirty = ref(false)
const campusSettingsSaving = ref(false)
const campusSettingsMessage = ref('')
const campusSettingsMessageTone = ref('success')
let campusStatusTimer = null
let campusCredentialsSaveTimer = null
let campusCredentialsRevision = 0

const CREDENTIALS_SAVE_DELAY_MS = 700

async function loadCampusStatus() {
  try {
    const nextStatus = await props.requestBackend('/api/plugins/colszunet/status')
    campusStatus.value = nextStatus
    if (!campusCredentialsDirty.value && !campusCredentialsSaving.value) {
      campusUsernameInput.value = nextStatus.username
      campusPasswordInput.value = ''
    }
    if (!campusIntervalDirty.value && !campusSettingsSaving.value) {
      campusIntervalInput.value = nextStatus.probe_interval_seconds
    }
    campusStatusError.value = ''
  } catch (error) {
    campusStatusError.value = error instanceof Error ? error.message : String(error)
  }
}

function markCampusCredentialsDirty() {
  campusCredentialsDirty.value = true
  campusCredentialsRevision += 1
  campusCredentialsMessage.value = ''
  if (campusCredentialsSaveTimer !== null) {
    window.clearTimeout(campusCredentialsSaveTimer)
  }
  campusCredentialsSaveTimer = window.setTimeout(() => {
    campusCredentialsSaveTimer = null
    saveCampusCredentials()
  }, CREDENTIALS_SAVE_DELAY_MS)
}

async function saveCampusCredentials() {
  const revision = campusCredentialsRevision
  campusCredentialsSaving.value = true
  campusCredentialsMessage.value = ''
  try {
    const nextStatus = await props.requestBackend('/api/plugins/colszunet/settings', {
      method: 'POST',
      body: JSON.stringify({
        username: campusUsernameInput.value,
        password: campusPasswordInput.value,
      }),
    })
    campusStatus.value = nextStatus
    if (revision !== campusCredentialsRevision) {
      return
    }
    campusCredentialsDirty.value = false
    campusPasswordInput.value = ''
    campusCredentialsMessageTone.value = 'success'
    campusCredentialsMessage.value = nextStatus.credentials_configured
      ? '账号密码已自动保存'
      : '已保存，请填写完整账号密码后启动监控'
  } catch (error) {
    if (revision !== campusCredentialsRevision) {
      return
    }
    campusCredentialsMessageTone.value = 'error'
    campusCredentialsMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    campusCredentialsSaving.value = false
  }
}

function markCampusIntervalDirty() {
  campusIntervalDirty.value = true
  campusSettingsMessage.value = ''
}

async function saveCampusSettings() {
  const seconds = Number(campusIntervalInput.value)
  if (!Number.isInteger(seconds) || seconds < 1) {
    campusSettingsMessageTone.value = 'error'
    campusSettingsMessage.value = '请输入大于等于 1 的整数秒数'
    return
  }

  campusSettingsSaving.value = true
  campusSettingsMessage.value = ''
  try {
    campusStatus.value = await props.requestBackend('/api/plugins/colszunet/settings', {
      method: 'POST',
      body: JSON.stringify({ probe_interval_seconds: seconds }),
    })
    campusIntervalDirty.value = false
    campusSettingsMessageTone.value = 'success'
    campusSettingsMessage.value = `已保存，每 ${seconds} 秒检查一次`
  } catch (error) {
    campusSettingsMessageTone.value = 'error'
    campusSettingsMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    campusSettingsSaving.value = false
  }
}

watch(
  () => props.plugin.enabled,
  () => {
    loadCampusStatus()
  },
)

onMounted(() => {
  loadCampusStatus()
  campusStatusTimer = window.setInterval(loadCampusStatus, 5000)
})

onBeforeUnmount(() => {
  if (campusStatusTimer !== null) {
    window.clearInterval(campusStatusTimer)
  }
  if (campusCredentialsSaveTimer !== null) {
    window.clearTimeout(campusCredentialsSaveTimer)
  }
})
</script>

<template>
  <div class="campus-status-content">
    <div v-if="campusStatusError" class="campus-status-error">
      <strong>校园网状态读取失败</strong>
      <span>{{ campusStatusError }}</span>
    </div>
    <div v-else-if="!campusStatus" class="campus-status-loading">
      <div class="loader"></div>
      <span>正在读取校园网监控状态</span>
    </div>
    <div v-else class="campus-status-grid">
      <article class="campus-status-card" :class="`tone-${campusStatus.current.tone}`">
        <div class="campus-status-heading">
          <span>{{ campusStatus.current.label }}</span>
          <strong>{{ campusStatus.current.status }}</strong>
        </div>
        <p>{{ campusStatus.current.detail }}</p>
        <small>{{ campusStatus.current.checked_at ?? '等待首次检测' }}</small>
      </article>

      <article class="campus-status-card" :class="`tone-${campusStatus.listener.tone}`">
        <div class="campus-status-heading">
          <span>{{ campusStatus.listener.label }}</span>
          <strong>{{ campusStatus.listener.status }}</strong>
        </div>
        <p>{{ campusStatus.listener.detail }}</p>
        <small>每 {{ campusStatus.probe_interval_seconds }} 秒检查一次</small>
      </article>

      <article class="campus-history-card">
        <div class="campus-status-heading">
          <span>24h / 7d 内状态</span>
          <strong>连通性记录</strong>
        </div>
        <div v-for="window in campusStatus.history" :key="window.label" class="history-row">
          <div class="history-row-heading">
            <span>{{ window.label }}</span>
            <strong>{{ window.success_rate === null ? '暂无' : `${window.success_rate}%` }}</strong>
          </div>
          <div
            class="history-status-bar"
            role="img"
            :aria-label="`${window.label}历史状态`"
          >
            <span
              v-for="(point, pointIndex) in window.points"
              :key="`${window.label}-${point.start_at}-${pointIndex}`"
              class="history-status-segment"
              :class="`tone-${point.tone}`"
              :title="`${point.start_at} 至 ${point.end_at} · ${point.status} · ${point.detail}`"
            ></span>
          </div>
          <p>{{ window.detail }}</p>
          <small>{{ window.status }} · {{ window.latest_at ?? '暂无最新记录' }}</small>
        </div>
        <div class="history-legend" aria-label="历史状态图例">
          <span class="history-legend-item"><i class="tone-online"></i>成功</span>
          <span class="history-legend-item"><i class="tone-offline"></i>失败</span>
          <span class="history-legend-item"><i class="tone-pending"></i>暂无记录</span>
        </div>
      </article>
    </div>
    <div v-if="campusStatus" class="campus-credentials-panel">
      <div class="campus-settings-copy">
        <strong>校园网账号密码</strong>
        <span>停止输入约 700 毫秒后自动保存</span>
      </div>
      <div class="campus-credentials-form">
        <label class="campus-credential-field">
          <span>账号</span>
          <input
            v-model="campusUsernameInput"
            type="text"
            autocomplete="username"
            aria-label="校园网账号"
            @input="markCampusCredentialsDirty"
          />
        </label>
        <label class="campus-credential-field">
          <span>密码</span>
          <input
            v-model="campusPasswordInput"
            type="password"
            autocomplete="current-password"
            placeholder="留空保持当前密码"
            aria-label="校园网密码"
            @input="markCampusCredentialsDirty"
          />
        </label>
        <span
          v-if="campusCredentialsSaving || campusCredentialsMessage"
          class="campus-settings-message"
          :class="`is-${campusCredentialsMessageTone}`"
        >
          {{ campusCredentialsSaving ? '保存中' : campusCredentialsMessage }}
        </span>
      </div>
    </div>
    <div v-if="campusStatus" class="campus-settings-panel">
      <div class="campus-settings-copy">
        <strong>检查间隔</strong>
        <span>设置 colszunet 检查网络连通性的时间间隔</span>
      </div>
      <form class="campus-settings-form" @submit.prevent="saveCampusSettings">
        <input
          v-model.number="campusIntervalInput"
          type="number"
          min="1"
          step="1"
          aria-label="检查间隔秒数"
          @input="markCampusIntervalDirty"
        />
        <span>秒</span>
        <button type="submit" :disabled="campusSettingsSaving">
          {{ campusSettingsSaving ? '保存中' : '保存设置' }}
        </button>
      </form>
      <span
        v-if="campusSettingsMessage"
        class="campus-settings-message"
        :class="`is-${campusSettingsMessageTone}`"
      >
        {{ campusSettingsMessage }}
      </span>
    </div>
  </div>
</template>
