<template>
  <section class="recalc-panel" data-testid="recalc-panel">
    <header class="panel-head">
      <div>
        <h3>重算依赖核验</h3>
        <p class="page-desc">
          按日期读取字段血缘，列出可执行来源与缺口；缺口日期不会产生已确认差异。
        </p>
      </div>
      <div class="panel-controls">
        <label class="filter-item">
          <span>重算任务</span>
          <select v-model.number="taskId" data-testid="recalc-task-select" @change="reload">
            <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
              #{{ row.id }} · {{ row['关联指标'] }} · {{ row['历史时间范围'] }}
            </option>
          </select>
        </label>
        <button class="btn" data-testid="recalc-verify" type="button" @click="reload">核对依赖</button>
        <button class="btn primary" data-testid="recalc-continue" type="button" :disabled="busy" @click="continueRecalc">
          {{ busy ? '处理中…' : '重算 / 继续' }}
        </button>
      </div>
    </header>

    <p v-if="message" class="panel-message" data-testid="recalc-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="recalc-error">{{ errorMessage }}</p>

    <template v-if="verification">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">依赖状态</span>
          <strong class="stat-value" data-testid="recalc-dependency-status">{{ verification['依赖状态'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">可复核天数</span>
          <strong class="stat-value">{{ verification['可复核天数'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">缺口天数</span>
          <strong class="stat-value" data-testid="recalc-gap-count">{{ verification['缺口天数'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已确认 / 待确认</span>
          <strong class="stat-value">
            {{ verification['已确认天数'] }} / {{ verification['待确认天数'] }}
          </strong>
        </article>
      </div>

      <h4>按日期分段</h4>
      <table class="data-table" data-testid="recalc-segments">
        <thead>
          <tr>
            <th>区间</th><th>天数</th><th>可复核</th><th>适用版本</th>
            <th>来源字段</th><th>转换逻辑</th><th>缺口</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(segment, index) in verification['分段']" :key="index" :data-status="segment['可复核'] ? '可复核' : '缺口'">
            <td>{{ segment['区间'] }}</td>
            <td>{{ segment['天数'] }}</td>
            <td>{{ segment['可复核'] ? '可复核' : '缺口' }}</td>
            <td>{{ segment['适用版本'] || '—' }}</td>
            <td>{{ (segment['来源字段列表'] || []).join('、') || '—' }}</td>
            <td>{{ segment['转换逻辑'] || '—' }}</td>
            <td>{{ segment['缺口'] || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h4>按日计入明细</h4>
      <table class="data-table" data-testid="recalc-days">
        <thead>
          <tr>
            <th>计量日期</th><th>适用版本</th><th>转换逻辑</th>
            <th>已确认</th><th>差异</th><th>计入次数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="day in verification['按日核验']" :key="day['计量日期']" :data-testid="`recalc-day-${day['计量日期']}`">
            <td>{{ day['计量日期'] }}</td>
            <td>{{ day['适用版本'] || '—' }}</td>
            <td>{{ day['转换逻辑'] || '—' }}</td>
            <td>{{ day['已确认'] ? '已确认' : (day['可复核'] ? '待计算' : '缺来源') }}</td>
            <td>{{ day['差异'] === null || day['差异'] === undefined ? '—' : `${day['差异']}%` }}</td>
            <td>{{ day['计入次数'] }}</td>
          </tr>
        </tbody>
      </table>

      <h4>已确认区间与差异</h4>
      <table v-if="verification['已确认区间'].length" class="data-table" data-testid="recalc-confirmed">
        <thead>
          <tr><th>区间</th><th>天数</th><th>适用版本</th><th>转换逻辑</th><th>差异合计</th></tr>
        </thead>
        <tbody>
          <tr v-for="(segment, index) in verification['已确认区间']" :key="index">
            <td>{{ segment['区间'] }}</td>
            <td>{{ segment['天数'] }}</td>
            <td>{{ segment['适用版本'] || '—' }}</td>
            <td>{{ segment['转换逻辑'] || '—' }}</td>
            <td>{{ segment['差异合计'] }}%</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="page-desc">尚无已确认区间，先补齐来源后执行重算。</p>

      <p v-if="verification['缺口日期'].length" class="gap-note" data-testid="recalc-gaps">
        缺口日期：{{ verification['缺口日期'].join('、') }}
      </p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import type { EntryRow } from '@/data/types'

type Segment = {
  区间: string
  天数: number
  可复核: boolean
  适用版本: string
  来源字段列表: string[]
  转换逻辑: string
  缺口: string | null
}

type DayDetail = {
  计量日期: string
  可复核: boolean
  已确认: boolean
  适用版本: string
  转换逻辑: string
  差异: number | null
  计入次数: number
}

type ConfirmedSegment = {
  区间: string
  天数: number
  适用版本: string
  转换逻辑: string
  差异合计: number
}

type Verification = {
  任务ID: number
  关联指标: string
  新口径版本: string
  历史时间范围: string
  依赖状态: string
  可复核天数: number
  缺口天数: number
  已确认天数: number
  待确认天数: number
  分段: Segment[]
  按日核验: DayDetail[]
  已确认区间: ConfirmedSegment[]
  缺口日期: string[]
  产出差异: string
  error?: string
}

const props = defineProps<{ rows: EntryRow[] }>()
const taskId = ref<number | null>(props.rows.length ? Number(props.rows[0].id) : null)
const verification = ref<Verification | null>(null)
const errorMessage = ref('')
const message = ref('')
const busy = ref(false)

watch(
  () => props.rows.map((row) => row.id),
  () => {
    if (taskId.value === null && props.rows.length) {
      taskId.value = Number(props.rows[0].id)
      void reload()
    }
  },
)

async function readVerification(): Promise<Verification> {
  const response = await fetch(`/api/cross/recalc/${taskId.value}/dependencies`)
  if (!response.ok) throw new Error(`依赖核验失败：HTTP ${response.status}`)
  return (await response.json()) as Verification
}

async function reload() {
  if (taskId.value === null) return
  errorMessage.value = ''
  try {
    const payload = await readVerification()
    if (payload.error) throw new Error(payload.error)
    verification.value = payload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '依赖核验失败'
  }
}

async function continueRecalc() {
  if (taskId.value === null) return
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await fetch(`/api/cross/recalc/${taskId.value}/start`, { method: 'POST' })
    if (!response.ok) throw new Error(`重算失败：HTTP ${response.status}`)
    const result = await response.json()
    if (result.error) throw new Error(result.error)
    message.value = result['产出差异'] || `重算完成，状态 ${result['状态']}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算失败'
  } finally {
    busy.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.recalc-panel {
  margin-top: 20px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}
.panel-head h3 { margin: 0; font-size: 15px; }
.panel-controls { display: flex; gap: 8px; align-items: flex-end; }
.panel-controls select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  min-width: 260px;
}
.recalc-panel h4 { font-size: 13px; margin: 16px 0 6px; }
.panel-message { font-size: 13px; color: #166534; background: #f0fdf4; border-radius: 6px; padding: 8px 10px; }
.gap-note { font-size: 13px; color: #b45309; }
</style>
