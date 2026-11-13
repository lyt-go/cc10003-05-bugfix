<template>
  <section class="page" data-module="datadrill">
    <header class="page-head">
      <div>
        <h2>多维下钻管理</h2>
        <p class="page-desc">下钻路径与查询模板按同一模板版本共同变化：启用前校验模板已启用、每层维度有来源、逐层度量口径一致；模板废弃后保留历史层级与缓存并阻止新聚合请求。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="!selectedIds.length" @click="batchEnable">批量启用（{{ selectedIds.length }}）</button>
        <button class="btn" type="button" @click="exportRows">导出多维下钻清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th style="width: 36px"><input type="checkbox" :checked="allSelected" @change="toggleAll" /></th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-invalid': row.失效来源 }">
          <td><input type="checkbox" :value="row.id" v-model="selectedIds" /></td>
          <td v-for="column in columns" :key="column">
            <span v-if="column === '版本一致'">
              <span :class="row.版本一致 ? 'ok-text' : 'warn-text'">{{ row.版本一致 ? '一致' : '已漂移' }}</span>
            </span>
            <span v-else-if="column === '失效来源'" :class="row.失效来源 ? 'error-text' : ''">{{ row.失效来源 || '—' }}</span>
            <span v-else>{{ row[column] ?? '—' }}</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openDetail(row)">详情/下钻</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无多维下钻数据，可先登记下钻路径</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条多维下钻记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="detail" class="detail-panel">
      <header class="detail-head">
        <h3>路径详情：{{ detail.路径编号 }}（{{ detail.status }}）</h3>
        <button class="btn ghost" type="button" @click="detail = null">关闭</button>
      </header>
      <p v-if="detail.失效来源" class="error-text">失效来源：{{ detail.失效来源 }}</p>
      <dl class="detail-grid">
        <div v-for="item in detailItems" :key="item.label">
          <dt>{{ item.label }}</dt>
          <dd>{{ item.value ?? '—' }}</dd>
        </div>
      </dl>

      <div class="detail-actions">
        <label>
          聚合层级
          <input v-model.number="queryLevel" type="number" min="1" :max="layerCount" style="width: 80px" />
        </label>
        <button class="btn primary" type="button" @click="runAggregate">发起聚合查询</button>
        <button class="btn" type="button" @click="rebindVersion">对齐模板最新版本</button>
        <button class="btn" type="button" @click="loadDetail(detail.id)">刷新</button>
      </div>
      <p v-if="aggregateMessage" :class="aggregateOk ? 'ok-text' : 'error-text'">{{ aggregateMessage }}</p>
      <table v-if="aggregateResult" class="data-table">
        <thead>
          <tr><th v-for="key in aggregateColumns" :key="key">{{ key }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="(r, idx) in aggregateResult.rows" :key="idx">
            <td v-for="key in aggregateColumns" :key="key">{{ r[key] }}</td>
          </tr>
        </tbody>
      </table>

      <h4>历史查询缓存（模板废弃后仍保留）</h4>
      <table v-if="cacheRows.length" class="data-table">
        <thead><tr><th>模板</th><th>版本</th><th>层级</th><th>生成时间</th><th>行数</th></tr></thead>
        <tbody>
          <tr v-for="(c, idx) in cacheRows" :key="idx">
            <td>{{ c.模板编号 }}</td><td>v{{ c.模板版本 }}</td><td>{{ c.level }}</td><td>{{ c.at }}</td><td>{{ c.rows.length }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty-state">暂无历史缓存</p>

      <h4>审计流水（可还原当时模板版本）</h4>
      <ul v-if="auditRows.length" class="audit-list">
        <li v-for="a in auditRows" :key="a.id">
          <strong>{{ a.at }} · {{ a.action }}</strong>
          <span v-if="a.template_snapshot">
            模板 {{ a.template_snapshot.模板编号 }} v{{ a.template_snapshot.模板版本 }}
            （{{ a.template_snapshot.状态 }}，维度：{{ a.template_snapshot.维度字段.join(',') }}，度量：{{ a.template_snapshot.度量字段.join(',') }}）
          </span>
          <span v-if="a.remark"> {{ a.remark }}</span>
        </li>
      </ul>
      <p v-else class="empty-state">暂无审计记录</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/datadrill'
const columns = ["路径编号", "起始维度", "下钻层级", "每层维度名", "每层度量", "路径深度", "关联看板", "关联模板编号", "模板版本快照", "模板当前版本", "模板当前状态", "版本一致", "失效来源"]
const actions = ["提交路径评审", "启用路径", "关闭路径"]
const stats = [{ "label": "下钻路径总数", "value": 0 }, { "label": "已启用路径", "value": 0 }, { "label": "路径评审中", "value": 0 }]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = ["路径编号", "起始维度", "下钻层级"]
const selectedIds = ref<number[]>([])

const allSelected = computed(() => rows.value.length > 0 && selectedIds.value.length === rows.value.length)
function toggleAll(event: Event) {
  selectedIds.value = (event.target as HTMLInputElement).checked ? rows.value.map((r) => Number(r.id)) : []
}

const detail = ref<Row | null>(null)
const queryLevel = ref(1)
const aggregateResult = ref<Row | null>(null)
const aggregateMessage = ref('')
const aggregateOk = ref(false)
const cacheRows = ref<Row[]>([])
const auditRows = ref<Row[]>([])

const layerCount = computed(() => String(detail.value?.下钻层级 ?? '').split(/[>＞]/).filter(Boolean).length || 1)
const detailItems = computed(() => {
  if (!detail.value) return []
  const labels = ["路径编号", "状态", "起始维度", "下钻层级", "每层维度名", "每层度量", "关联模板编号", "模板版本快照", "模板当前版本", "模板当前状态", "启用时间", "失效来源"]
  return labels.map((label) => ({ label, value: label === '状态' ? detail.value?.status : detail.value?.[label] }))
})
const aggregateColumns = computed(() => {
  if (!aggregateResult.value) return []
  return ['层级', ...(aggregateResult.value.维度 as string[]), ...(aggregateResult.value.度量 as string[])]
})

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function readMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json()
    return body?.message || fallback
  } catch {
    return fallback
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const body = await response.json()
    if (!response.ok || body.ok === false) {
      errorMessage.value = body?.message || '多维下钻动作未生效，请稍后重试'
    }
    await reload()
    if (detail.value && Number(detail.value.id) === Number(row.id)) await loadDetail(row.id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '多维下钻操作失败'
  }
}

async function batchEnable() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/batch-actions`, {
      method: 'POST',
      body: JSON.stringify({ action: '启用路径', entry_ids: selectedIds.value }),
    })
    const body = await response.json()
    if (!response.ok || body.ok === false) {
      const perItem = (body.results || []).map((r: Row) => `${r.路径编号 ?? r.entry_id}：${r.ok ? '通过' : r.message}`).join('；')
      errorMessage.value = `${body.message || '整组校验未通过'}${perItem ? `（${perItem}）` : ''}`
    } else {
      errorMessage.value = body.message || '批量启用成功'
    }
    selectedIds.value = []
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量启用失败'
  }
}

async function openDetail(row: Row) {
  aggregateResult.value = null
  aggregateMessage.value = ''
  queryLevel.value = 1
  await loadDetail(row.id)
}

async function loadDetail(id: number) {
  const [detailRes, cacheRes, auditRes] = await Promise.all([
    request(`${ENDPOINT}/${id}`),
    request(`${ENDPOINT}/${id}/caches`),
    request(`${ENDPOINT}/${id}/audit`),
  ])
  detail.value = await detailRes.json()
  const cacheBody = await cacheRes.json()
  cacheRows.value = cacheBody.items ?? []
  const auditBody = await auditRes.json()
  auditRows.value = auditBody.items ?? []
}

async function runAggregate() {
  aggregateMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${detail.value?.id}/aggregate`, {
      method: 'POST',
      body: JSON.stringify({ values: { level: queryLevel.value } }),
    })
    const body = await response.json()
    aggregateOk.value = !!body.ok
    aggregateMessage.value = body.message || ''
    aggregateResult.value = body.ok ? body.entry.result : null
    if (body.ok) await loadDetail(Number(detail.value?.id))
  } catch (error) {
    aggregateOk.value = false
    aggregateMessage.value = error instanceof Error ? error.message : '聚合查询失败'
  }
}

async function rebindVersion() {
  aggregateMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${detail.value?.id}/rebind-version`, { method: 'POST', body: '{}' })
    const body = await response.json()
    aggregateOk.value = !!body.ok
    aggregateMessage.value = body.message || ''
    await reload()
    if (detail.value) await loadDetail(Number(detail.value.id))
  } catch (error) {
    aggregateOk.value = false
    aggregateMessage.value = error instanceof Error ? error.message : '版本对齐失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error(await readMessage(response, '下钻路径列表读取失败'))
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats[0].value = total.value
    stats[1].value = rows.value.filter((r) => r.status === '已启用').length
    stats[2].value = rows.value.filter((r) => r.status === '路径评审中').length
    selectedIds.value = selectedIds.value.filter((id) => rows.value.some((r) => Number(r.id) === id))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '多维下钻列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.row-invalid { background: #fff7f6; }
.ok-text { color: #067647; }
.warn-text { color: #b54708; }
.detail-panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.detail-head { display: flex; justify-content: space-between; align-items: center; }
.detail-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px 16px; margin: 8px 0; }
.detail-grid dt { font-size: 12px; color: var(--muted); }
.detail-grid dd { margin: 2px 0 0; font-size: 13px; }
.detail-actions { display: flex; gap: 10px; align-items: flex-end; margin: 10px 0; flex-wrap: wrap; }
.audit-list { list-style: none; padding: 0; margin: 0; font-size: 13px; }
.audit-list li { padding: 6px 0; border-bottom: 1px dashed var(--border); display: flex; flex-direction: column; gap: 2px; }
</style>
