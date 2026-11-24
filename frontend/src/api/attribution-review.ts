/** 投放复盘接口：归因权重有效归属、更正、领用和月末核对导出。 */
import { request } from './client'

export type GroupDetail = {
  group: string
  组别: string
  effective_from: string
  effective_to: string
  order_count: number
  订单数: number
  weight: number
  归因权重: number
  confirmed_amount: number
  已确认分摊: number
  reversal_amount: number
  冲减: number
  supplement_amount: number
  补记: number
  ledger_net: number
  pending_correction: number
  待处理更正金额: number
  original_orders: string[]
  status: string
}

export type LedgerEntry = {
  entry_id: string
  order_id: string
  group: string
  类型: string
  amount: number
  金额: number
  status: string
  active: boolean
  correction_id: string | null
  note: string
}

export type OrderDetail = {
  order_id: string
  原订单: string
  channel: string
  原渠道: string
  conversion_time: string
  转化时间: string
  amount: number
  归因权重: number
  current_group: string
  当前有效组: string
  original_group: string
  原归属组: string
  claimed: boolean
  claim: ClaimRecord | null
  active_entry: LedgerEntry | null
  entries: LedgerEntry[]
}

export type ClaimRecord = {
  claim_id: string
  order_id: string
  group: string
  组别: string
  claimed_at: string
  amount: number
  status: string
  active: boolean
  restored_at: string | null
  correction_id: string | null
}

export type CorrectionRecord = {
  correction_id: string
  channel: string
  effective_at: string
  生效时间: string
  operated_at: string
  reason: string
  reverted_claims: string[]
  entries: string[]
}

export type PendingSummary = {
  by_group: Record<string, number>
  groups: Record<string, { amount: number; has_pending: boolean }>
  has_pending: boolean
}

export type ReviewOverview = {
  channel: string
  渠道: string
  period: { start: string; end: string; start_date: string; end_date: string }
  handover: {
    original_effective_at: string
    effective_at: string
    corrected: boolean
    corrections: CorrectionRecord[]
  }
  groups: GroupDetail[]
  orders: OrderDetail[]
  claims: ClaimRecord[]
  current_owner: Record<string, string>
  totals: {
    attribution_weight: number
    本期合计: number
    channel_touchpoints: number
    旧渠道触点合计: number
    reconciled: boolean
    对平: boolean
  }
  pending_corrections: PendingSummary
  reconciliation: { balanced: boolean; duplicate_compensation_count: number }
  valid_groups: string[]
}

export type ReconciliationOrder = {
  order_id: string
  原订单: string
  conversion_time: string
  转化时间: string
  weight: number
  归因权重: number
  current_group: string
  shares: Record<string, number>
  group_shares: Record<string, number>
  ledger_total: number
  balanced: boolean
}

export type Reconciliation = {
  channel: string
  period: { start: string; end: string }
  groups: GroupDetail[]
  orders: ReconciliationOrder[]
  duplicate_compensation_count: number
  balanced: boolean
}

export type OwnerAnswer = {
  ok: boolean
  order_id: string
  current_group: string
  当前有效组: string
  original_group: string
  amount: number
  claim: ClaimRecord | null
  other_group: string
  other_pending_correction: { amount: number; has_pending: boolean }
}

export type ExportRows = Record<string, Record<string, string | number>[]>

export class ReviewError extends Error {
  payload: unknown
  constructor(message: string, payload: unknown) {
    super(message)
    this.name = 'ReviewError'
    this.payload = payload
  }
}

async function readJson<T>(response: Response): Promise<T> {
  const payload = await response.json()
  if (!response.ok) {
    const detail = payload?.detail
    const message = typeof detail === 'string'
      ? detail
      : detail?.message ?? detail?.detail ?? `接口返回 ${response.status}`
    throw new ReviewError(String(message), detail)
  }
  return payload as T
}

function query(params: Record<string, string | undefined>): string {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value) search.set(key, value)
  })
  const text = search.toString()
  return text ? `?${text}` : ''
}

export async function loadOverview(group?: string): Promise<ReviewOverview> {
  return readJson<ReviewOverview>(
    await request(`/api/attribution-review${query({ group })}`),
  )
}

export async function loadReconciliation(group?: string): Promise<Reconciliation> {
  return readJson<Reconciliation>(
    await request(`/api/attribution-review/reconciliation${query({ group })}`),
  )
}

export async function loadOrder(orderId: string): Promise<OwnerAnswer> {
  return readJson<OwnerAnswer>(
    await request(`/api/attribution-review/orders/${encodeURIComponent(orderId.trim())}`),
  )
}

export async function claimOrder(
  orderId: string,
  group: string,
  claimedAt?: string,
): Promise<{ ok: boolean; message: string; claim: ClaimRecord }> {
  return readJson(
    await request('/api/attribution-review/claims', {
      method: 'POST',
      body: JSON.stringify({ order_id: orderId, group, claimed_at: claimedAt }),
    }),
  )
}

export async function restoreClaim(
  claimId: string,
  operatedAt?: string,
): Promise<{ ok: boolean; message: string; claim: ClaimRecord }> {
  return readJson(
    await request('/api/attribution-review/claims/restore', {
      method: 'POST',
      body: JSON.stringify({ claim_id: claimId, operated_at: operatedAt }),
    }),
  )
}

export async function correctHandover(payload: {
  effective_at: string
  correction_id?: string
  operated_at?: string
  reason?: string
}): Promise<{ ok: boolean; message: string; replayed: boolean; reverted_claims: string[] }> {
  return readJson(
    await request('/api/attribution-review/corrections', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  )
}

export async function processPending(payload: {
  group?: string
  order_id?: string
}): Promise<{ ok: boolean; message: string; count: number; pending: PendingSummary }> {
  return readJson(
    await request('/api/attribution-review/pending/process', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  )
}

export async function loadExportRows(
  group: string,
  columns: string[],
): Promise<{ items: ExportRows; files: Record<string, string> }> {
  return readJson(
    await request(`/api/attribution-review/export${query({
      group,
      columns: columns.join(','),
      format: 'json',
    })}`),
  )
}

export async function downloadExport(
  group: string,
  columns: string[],
  kind?: string,
): Promise<void> {
  const response = await request(`/api/attribution-review/export${query({
    group,
    kind,
    columns: columns.join(','),
  })}`)
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new ReviewError(payload?.detail ?? '核对文件导出失败', payload)
  }
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = kind ? `C1-${group}-${kind}.csv` : `C1-${group}-月末核对.zip`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function resetLedger(): Promise<void> {
  const response = await request('/api/attribution-review/reset', {
    method: 'POST',
    body: JSON.stringify({}),
  })
  if (!response.ok) throw new ReviewError('投放复盘账本重置失败', null)
}
