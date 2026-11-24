<template>
  <section class="page review-page">
    <header class="page-head">
      <div>
        <h2>投放复盘</h2>
        <p class="page-desc">
          C1 归因权重按有效归属加工组别明细；领用、更正与月末核对都以当前有效组为准。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="review-refresh" type="button" @click="reload">刷新账本</button>
        <button class="btn ghost" data-testid="review-reset" type="button" @click="reset">恢复示例</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">本期归因权重</span>
        <strong class="stat-value" data-testid="total-weight">{{ totals.attribution_weight ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">旧渠道触点合计</span>
        <strong class="stat-value" data-testid="total-touchpoints">{{ totals.channel_touchpoints ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">本期对平</span>
        <strong class="stat-value" data-testid="total-balanced">{{ totals.reconciled ? '是' : '否' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">移交时点</span>
        <strong class="stat-value small" data-testid="handover-at">{{ handoverText }}</strong>
      </article>
    </div>

    <p v-if="errorMessage" class="error-text" data-testid="review-error">{{ errorMessage }}</p>
    <p v-if="noticeMessage" class="notice-text" data-testid="review-notice">{{ noticeMessage }}</p>

    <h3 class="section-title">组别明细（按生效时间聚合）</h3>
    <table class="data-table" data-testid="group-table">
      <thead>
        <tr>
          <th>组别</th>
          <th>生效区间</th>
          <th>订单数</th>
          <th>归因权重</th>
          <th>已确认分摊</th>
          <th>冲减</th>
          <th>补记</th>
          <th>账实净额</th>
          <th>待处理更正金额</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in overview?.groups ?? []" :key="row.group" :data-testid="`group-${row.group}`">
          <td>{{ row.group }}</td>
          <td>{{ row.effective_from }} ~ {{ row.effective_to }}</td>
          <td>{{ row.order_count }}</td>
          <td>{{ row.weight }}</td>
          <td>{{ row.confirmed_amount }}</td>
          <td>{{ row.reversal_amount }}</td>
          <td>{{ row.supplement_amount }}</td>
          <td>{{ row.ledger_net }}</td>
          <td>{{ row.pending_correction }}</td>
        </tr>
      </tbody>
    </table>

    <div class="review-columns">
      <section class="review-panel">
        <h3 class="section-title">移交时点更正</h3>
        <form class="analysis-form" @submit.prevent="submitCorrection">
          <label>
            <span>更正生效时间</span>
            <input v-model="correction.effective_at" data-testid="correction-effective-at" placeholder="2026-11-17T12:00:00" />
          </label>
          <label>
            <span>更正编号</span>
            <input v-model="correction.correction_id" data-testid="correction-id" placeholder="COR-FIN-01" />
          </label>
          <label>
            <span>原因</span>
            <input v-model="correction.reason" data-testid="correction-reason" placeholder="财务更正移交时点" />
          </label>
          <button class="btn primary" data-testid="correction-submit" type="submit" :disabled="busy">应用更正</button>
        </form>
        <p class="hint">
          更正会让已确认的归属可还原：原组生成冲减、新组生成补记，同一笔权重只进入一个有效组。
        </p>
        <table class="data-table" v-if="corrections.length">
          <thead>
            <tr><th>更正编号</th><th>生效时间</th><th>还原领用</th><th>核对分录</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in corrections" :key="item.correction_id">
              <td>{{ item.correction_id }}</td>
              <td>{{ item.effective_at }}</td>
              <td>{{ item.reverted_claims.join('、') || '—' }}</td>
              <td>{{ item.entries.join('、') || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="review-panel">
        <h3 class="section-title">当前领用复核</h3>
        <form class="analysis-form" @submit.prevent="lookupOrder">
          <label>
            <span>订单号</span>
            <input v-model="orderQuery" data-testid="owner-order" placeholder="ORD-1001" />
          </label>
          <button class="btn" data-testid="owner-submit" type="submit" :disabled="busy">查当前领用组</button>
        </form>
        <dl v-if="ownerAnswer" class="owner-answer" data-testid="owner-answer">
          <dt>当前有效组</dt>
          <dd>{{ ownerAnswer.current_group }}</dd>
          <dt>原归属组</dt>
          <dd>{{ ownerAnswer.original_group }}</dd>
          <dt>有效领用</dt>
          <dd>{{ ownerAnswer.claim ? `${ownerAnswer.claim.group} @ ${ownerAnswer.claim.claimed_at}` : '尚未领用' }}</dd>
          <dt>另一组待处理更正</dt>
          <dd>
            {{ ownerAnswer.other_group }}：{{ ownerAnswer.other_pending_correction.amount }}
            {{ ownerAnswer.other_pending_correction.has_pending ? '（待处理）' : '（无待处理）' }}
          </dd>
        </dl>
        <p class="hint">领用权限随有效归属核定：只有当前有效组能领取该订单的归因权重。</p>
      </section>
    </div>

    <h3 class="section-title">本期订单与领用明细</h3>
    <table class="data-table" data-testid="order-table">
      <thead>
        <tr>
          <th>原订单</th>
          <th>原渠道</th>
          <th>转化时间</th>
          <th>归因权重</th>
          <th>当前有效组</th>
          <th>原归属组</th>
          <th>有效领用</th>
          <th>领用操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="order in overview?.orders ?? []" :key="order.order_id" :data-testid="`order-${order.order_id}`">
          <td>{{ order.order_id }}</td>
          <td>{{ order.channel }}</td>
          <td>{{ order.conversion_time }}</td>
          <td>{{ order.amount }}</td>
          <td>{{ order.current_group }}</td>
          <td>{{ order.original_group }}</td>
          <td>
            <span v-if="order.claim">{{ order.claim.group }} / {{ order.claim.claimed_at }}</span>
            <span v-else>—</span>
          </td>
          <td class="row-actions">
            <button
              class="link"
              v-for="group in groups"
              :key="group"
              type="button"
              :data-testid="`claim-${order.order_id}-${group}`"
              @click="claim(order.order_id, group)"
            >
              {{ group }}领用
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">待处理更正金额</h3>
    <table class="data-table" data-testid="pending-table">
      <thead>
        <tr><th>组别</th><th>待处理更正金额</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="group in groups" :key="group" :data-testid="`pending-${group}`">
          <td>{{ group }}</td>
          <td>{{ pending.groups?.[group]?.amount ?? 0 }}</td>
          <td>
            <button class="link" type="button" :data-testid="`process-${group}`" @click="process(group)">
              处理 {{ group }} 待更正
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">月末核对导出</h3>
    <form class="analysis-form" @submit.prevent="submitExport">
      <label>
        <span>有效组</span>
        <select v-model="exportForm.group" data-testid="export-group" @change="refreshPreview">
          <option v-for="group in validGroups" :key="group" :value="group">{{ group }}</option>
        </select>
      </label>
      <label>
        <span>核对类型</span>
        <select v-model="exportForm.kind" data-testid="export-kind" @change="refreshPreview">
          <option v-for="kind in exportKinds" :key="kind" :value="kind">{{ kind }}</option>
        </select>
      </label>
      <label class="check-group">
        <span>导出列</span>
        <label v-for="column in columns" :key="column" class="check-item">
          <input
            type="checkbox"
            :value="column"
            :checked="exportForm.columns.includes(column)"
            :data-testid="`export-column-${column}`"
            @change="toggleColumn(column); refreshPreview()"
          />
          {{ column }}
        </label>
      </label>
      <button class="btn primary" data-testid="export-download" type="submit" :disabled="busy || !exportForm.columns.length">
        导出核对文件
      </button>
    </form>
    <p class="hint">
      导出按转化时间排序；不选列时默认导出组别、金额、原订单三列。
    </p>

    <table class="data-table" v-if="exportRows.length" data-testid="export-preview">
      <thead>
        <tr><th v-for="column in exportColumns" :key="column">{{ column }}</th></tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in exportRows" :key="index" :data-testid="`export-row-${index}`">
          <td v-for="column in exportColumns" :key="column">{{ row[column] }}</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">月末逐单对账</h3>
    <p class="hint">
      <span data-testid="recon-balanced">{{ reconciliation?.balanced ? '两组逐单对平' : '存在未对平订单' }}</span>
      · 重复补偿记录：<span data-testid="recon-duplicates">{{ reconciliation?.duplicate_compensation_count ?? 0 }}</span>
    </p>
    <table class="data-table" data-testid="recon-table">
      <thead>
        <tr>
          <th>原订单</th>
          <th>转化时间</th>
          <th>归因权重</th>
          <th v-for="group in groups" :key="group">{{ group }}分摊</th>
          <th>账实合计</th>
          <th>对平</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="order in reconciliation?.orders ?? []" :key="order.order_id" :data-testid="`recon-${order.order_id}`">
          <td>{{ order.order_id }}</td>
          <td>{{ order.conversion_time }}</td>
          <td>{{ order.weight }}</td>
          <td v-for="group in groups" :key="group">{{ order.shares[group] ?? 0 }}</td>
          <td>{{ order.ledger_total }}</td>
          <td>{{ order.balanced ? '是' : '否' }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  claimOrder,
  correctHandover,
  downloadExport,
  loadExportRows,
  loadOverview,
  loadReconciliation,
  loadOrder,
  processPending,
  resetLedger,
  ReviewError,
  type ExportRows,
  type OwnerAnswer,
  type Reconciliation,
  type ReviewOverview,
} from '@/api/attribution-review'

const groups = ['甲组', '乙组']
const kinds = ['已确认分摊', '冲减', '补记']
const exportKinds = [...kinds, '全部分录（打包）']
const columns = ['组别', '金额', '原订单'] as const

const overview = ref<ReviewOverview | null>(null)
const reconciliation = ref<Reconciliation | null>(null)
const ownerAnswer = ref<OwnerAnswer | null>(null)
const exportRows = ref<Record<string, string | number>[]>([])
const errorMessage = ref('')
const noticeMessage = ref('')
const busy = ref(false)
const orderQuery = ref('ORD-1001')
const correction = reactive({
  effective_at: '2026-11-17T12:00:00',
  correction_id: 'COR-FIN-01',
  reason: '财务更正移交时点',
})
const exportForm = reactive<{ group: string; kind: string; columns: string[] }>({
  group: '甲组',
  kind: '已确认分摊',
  columns: ['组别', '金额', '原订单'],
})

const totals = computed(() => overview.value?.totals ?? {
  attribution_weight: 0,
  channel_touchpoints: 0,
  reconciled: false,
  '本期合计': 0,
  '旧渠道触点合计': 0,
  '对平': false,
})
const pending = computed(() => overview.value?.pending_corrections ?? {
  by_group: {},
  groups: {},
  has_pending: false,
})
const corrections = computed(() => overview.value?.handover.corrections ?? [])
const validGroups = computed(() => overview.value?.valid_groups ?? groups)
const handoverText = computed(() => {
  const handover = overview.value?.handover
  if (!handover) return '—'
  return `${handover.effective_at}${handover.corrected ? '（已更正）' : ''}`
})
const exportColumns = computed(() => exportForm.columns.length ? exportForm.columns : ['组别', '金额', '原订单'])

function message(error: unknown, fallback: string): string {
  if (error instanceof ReviewError) return error.message
  return error instanceof Error ? error.message : fallback
}

async function reload() {
  errorMessage.value = ''
  try {
    const [payload, recon] = await Promise.all([
      loadOverview(),
      loadReconciliation(),
    ])
    overview.value = payload
    reconciliation.value = recon
    if (!validGroups.value.includes(exportForm.group)) exportForm.group = validGroups.value[0] ?? '甲组'
    await refreshPreview()
  } catch (error) {
    errorMessage.value = message(error, '投放复盘读取失败')
  }
}

function toggleColumn(column: string) {
  const index = exportForm.columns.indexOf(column)
  if (index >= 0) exportForm.columns.splice(index, 1)
  else exportForm.columns.push(column)
}

async function refreshPreview() {
  if (!exportForm.group || !exportForm.columns.length) {
    exportRows.value = []
    return
  }
  try {
    const payload = await loadExportRows(exportForm.group, exportForm.columns)
    if (exportForm.kind === '全部分录（打包）') {
      exportRows.value = Object.values(payload.items ?? {}).flat() as Record<string, string | number>[]
    } else {
      exportRows.value = (payload.items?.[exportForm.kind] ?? []) as Record<string, string | number>[]
    }
  } catch {
    exportRows.value = []
  }
}

async function submitCorrection() {
  busy.value = true
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const result = await correctHandover({
      effective_at: correction.effective_at,
      correction_id: correction.correction_id || undefined,
      reason: correction.reason || undefined,
    })
    noticeMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = message(error, '更正失败')
  } finally {
    busy.value = false
  }
}

async function claim(orderId: string, group: string) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const result = await claimOrder(orderId, group)
    noticeMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = message(error, '领用失败')
  }
}

async function lookupOrder() {
  busy.value = true
  errorMessage.value = ''
  try {
    ownerAnswer.value = await loadOrder(orderQuery.value)
  } catch (error) {
    ownerAnswer.value = null
    errorMessage.value = message(error, '订单当前领用复核失败')
  } finally {
    busy.value = false
  }
}

async function process(group: string) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const result = await processPending({ group })
    noticeMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = message(error, '待更正金额处理失败')
  }
}

async function submitExport() {
  busy.value = true
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const packaged = exportForm.kind === '全部分录（打包）'
    await downloadExport(exportForm.group, exportForm.columns, packaged ? undefined : exportForm.kind)
    noticeMessage.value = packaged
      ? `已打包导出 ${exportForm.group} 的已确认分摊、冲减、补记三份核对文件`
      : `已导出 ${exportForm.group} 的${exportForm.kind}核对文件`
  } catch (error) {
    errorMessage.value = message(error, '核对文件导出失败')
  } finally {
    busy.value = false
  }
}

async function reset() {
  busy.value = true
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    await resetLedger()
    noticeMessage.value = '投放复盘账本已恢复为初始示例'
    ownerAnswer.value = null
    await reload()
  } catch (error) {
    errorMessage.value = message(error, '账本恢复失败')
  } finally {
    busy.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.review-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.review-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.section-title { margin: 18px 0 8px; font-size: 15px; }
.hint { color: var(--muted); font-size: 12px; }
.notice-text { color: #18794e; font-size: 13px; }
.owner-answer { display: grid; grid-template-columns: 120px 1fr; gap: 6px 8px; font-size: 13px; }
.owner-answer dt { color: var(--muted); }
.owner-answer dd { margin: 0; }
.check-group { flex-direction: row; align-items: center; gap: 8px; }
.check-item { display: inline-flex; align-items: center; gap: 4px; color: #1f2937; font-size: 13px; }
.stat-value.small { font-size: 14px; }
</style>
