<template>
  <section class="page" data-module="queryboard">
    <header class="page-head">
      <div>
        <h2>聚合查询管理</h2>
        <p class="page-desc">查询模板只能按草稿→模板验证中→已启用→已废弃推进；版本替换走乐观并发，下钻路径按同一模板版本共同变化，审计可还原每个版本当时的口径。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出聚合查询清单</button>
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
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-deprecated': row.status === '已废弃' }">
          <td v-for="column in columns" :key="column">
            <span v-if="column === '状态'">
              <span :class="row.status === '已启用' ? 'ok-text' : row.status === '已废弃' ? 'warn-text' : ''">{{ row[column] }}</span>
            </span>
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
            <button class="link" type="button" @click="publishVersion(row)">替换模板版本</button>
            <button class="link" type="button" @click="openAudit(row)">版本审计</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无聚合查询数据，可先登记查询模板</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条聚合查询记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="auditTarget" class="audit-panel">
      <header class="audit-head">
        <h3>版本审计：{{ auditTarget.模板编号 }}（当前 v{{ auditTarget.模板版本 }} · {{ auditTarget.status }}）</h3>
        <button class="btn ghost" type="button" @click="auditTarget = null">关闭</button>
      </header>
      <ul v-if="auditRows.length" class="audit-list">
        <li v-for="a in auditRows" :key="a.id">
          <strong>{{ a.at }} · {{ a.action }}</strong>
          <span v-if="a.template_snapshot">
            v{{ a.template_snapshot.模板版本 }}（{{ a.template_snapshot.状态 }}）
            维度：{{ a.template_snapshot.维度字段.join(',') }} ｜ 度量：{{ a.template_snapshot.度量字段.join(',') }}
            ｜ 数据源：{{ a.template_snapshot.数据源 }}
          </span>
          <span v-if="a.previous_snapshot">基于 v{{ a.previous_snapshot.模板版本 }} 替换</span>
          <span v-if="a.remark"> {{ a.remark }}</span>
        </li>
      </ul>
      <p v-else class="empty-state">暂无审计记录</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/queryboard'
const columns = ["模板编号", "状态", "模板版本", "指标名称", "维度字段", "度量字段", "排序规则", "分页上限", "缓存秒数", "数据源", "版本说明"]
const actions = ["提交模板验证", "启用模板", "废弃模板"]
const stats = [{ "label": "查询模板总数", "value": 0 }, { "label": "已启用模板", "value": 0 }, { "label": "模板验证中", "value": 0 }]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = ["模板编号", "指标名称", "维度字段"]
const auditTarget = ref<Row | null>(null)
const auditRows = ref<Row[]>([])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
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
      errorMessage.value = body?.message || '聚合查询动作未生效，请稍后重试'
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '聚合查询操作失败'
  }
}

async function publishVersion(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/versions`, {
      method: 'POST',
      // 带上当前版本做乐观锁：并发替换时只有一次能成功。
      body: JSON.stringify({ expected_version: Number(row.模板版本), values: {} }),
    })
    const body = await response.json()
    if (!response.ok || body.ok === false) {
      errorMessage.value = body?.message || '模板版本替换失败'
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '模板版本替换失败'
  }
}

async function openAudit(row: Row) {
  auditTarget.value = row
  const response = await request(`${ENDPOINT}/${row.id}/audit`)
  const body = await response.json()
  auditRows.value = body.items ?? []
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('查询模板列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats[0].value = total.value
    stats[1].value = rows.value.filter((r) => r.status === '已启用').length
    stats[2].value = rows.value.filter((r) => r.status === '模板验证中').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '聚合查询列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.row-deprecated { background: #fffaeb; }
.ok-text { color: #067647; }
.warn-text { color: #b54708; }
.audit-panel { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.audit-head { display: flex; justify-content: space-between; align-items: center; }
.audit-list { list-style: none; padding: 0; margin: 0; font-size: 13px; }
.audit-list li { padding: 6px 0; border-bottom: 1px dashed var(--border); display: flex; flex-direction: column; gap: 2px; }
</style>
