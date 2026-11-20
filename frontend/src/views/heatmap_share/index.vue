<template>
  <section class="page" data-module="heatmap_share">
    <header class="page-head">
      <div>
        <h2>授权热力共享</h2>
        <p class="page-desc">从带门店/区域归属的空间事件明细聚合授权热力，按读者当前许可交付门店或区域口径；门店许可撤回后旧链接即时失效，区域口径只交付区域格值与区域色阶。</p>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <h3 class="section-title">读取许可</h3>
    <table class="data-table">
      <thead>
        <tr><th>许可编号</th><th>读者账号</th><th>读者名称</th><th>粒度</th><th>授权范围</th><th>关联事件</th><th>状态</th><th>可执行动作</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in grants" :key="row.grant_id" :data-status="row.status">
          <td>{{ row.grant_id }}</td>
          <td>{{ row.reader_id }}</td>
          <td>{{ row.reader_name }}</td>
          <td>{{ row.grain === 'store' ? '门店' : '区域' }}</td>
          <td>{{ row.scope_name }}（{{ row.scope_id }}）</td>
          <td>{{ row.event_id }}</td>
          <td :class="row.status === '已生效' ? 'trend-up' : 'error-text'">{{ row.status }}</td>
          <td class="row-actions">
            <button v-if="row.status === '已生效'" class="link" type="button"
                    @click="revokeGrant(row.grant_id)">撤回许可</button>
            <span v-else class="empty-state">已失效</span>
          </td>
        </tr>
      </tbody>
    </table>

    <form class="filter-bar" @submit.prevent="submitGrant">
      <label class="filter-item"><span>读者账号</span><input v-model="grantForm.reader_id" required /></label>
      <label class="filter-item"><span>读者名称</span><input v-model="grantForm.reader_name" /></label>
      <label class="filter-item"><span>粒度</span>
        <select v-model="grantForm.grain">
          <option value="store">门店</option>
          <option value="region">区域</option>
        </select>
      </label>
      <label class="filter-item"><span>授权范围</span>
        <select v-model="grantForm.scope_id">
          <template v-if="grantForm.grain === 'store'">
            <option v-for="s in storeOptions" :key="s.store_id" :value="s.store_id">
              {{ s.store_name }}（{{ s.store_id }}）
            </option>
          </template>
          <template v-else>
            <option v-for="r in regionOptions" :key="r.region_id" :value="r.region_id">
              {{ r.region_name }}（{{ r.region_id }}）
            </option>
          </template>
        </select>
      </label>
      <button class="btn primary" type="submit">登记许可</button>
    </form>

    <h3 class="section-title">共享看板（旧链接）</h3>
    <table class="data-table">
      <thead>
        <tr><th>共享编号</th><th>名称</th><th>粒度</th><th>范围</th><th>色阶</th><th>链接 Token</th><th>状态</th><th>可执行动作</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in shares" :key="row.share_id" :data-status="row.status">
          <td>{{ row.share_id }}</td>
          <td>{{ row.share_name }}</td>
          <td>{{ row.grain === 'store' ? '门店' : '区域' }}</td>
          <td>{{ row.scope_name }}</td>
          <td>{{ row.color_scale }}</td>
          <td><code>{{ row.token }}</code></td>
          <td :class="row.status === '已发布' ? 'trend-up' : 'error-text'">{{ row.status }}</td>
          <td class="row-actions">
            <button v-if="row.status === '已发布'" class="link" type="button"
                    @click="revokeShare(row.share_id)">撤回共享</button>
            <span v-else class="empty-state">链接已失效</span>
          </td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">链接读取核验（模拟读者打开旧链接）</h3>
    <form class="filter-bar" @submit.prevent="simulateAccess">
      <label class="filter-item"><span>读者账号</span><input v-model="accessForm.reader_id" required /></label>
      <label class="filter-item"><span>共享 Token</span><input v-model="accessForm.token" required /></label>
      <button class="btn primary" type="submit">打开链接</button>
    </form>

    <div v-if="accessResult" class="result-box" :class="accessResult.ok ? 'result-ok' : 'result-bad'">
      <template v-if="accessResult.ok">
        <p><strong>可交付统计粒度：</strong>{{ accessResult.data.delivered_grain === 'store' ? '门店' : '区域' }}
          ｜ {{ accessResult.data.scope_name }}（{{ accessResult.data.scope_id }}）</p>
        <p><strong>色阶来源：</strong>授权热力数据 {{ accessResult.data.color_scale }}，
          下界 {{ accessResult.data.min_intensity }} / 上界 {{ accessResult.data.max_intensity }}（仅基于可见格值）</p>
        <p><strong>格数 / 计数：</strong>{{ accessResult.data.cell_count }} 格 ｜ 合计 {{ accessResult.data.total_count }}
          ｜ 依据许可 {{ accessResult.data.grants_used.join('、') }}</p>
        <p class="empty-state">响应不含任何门店级标识：{{ leakCheck ? '检测到门店编号（异常）' : '通过' }}</p>
      </template>
      <template v-else>
        <p><strong>链接不可用：</strong>HTTP {{ accessResult.status }} ｜ {{ accessResult.error.code }}</p>
        <p>{{ accessResult.error.message }}</p>
        <p v-if="accessResult.error.available_grain">
          当前可用粒度：{{ accessResult.error.available_grain === 'region' ? '区域' : '门店' }}
          ｜ 区域复核需改用区域级共享链接
        </p>
      </template>
    </div>

    <footer class="page-foot">
      <span>许可在每次访问时按当前授权重新校验，撤回即时生效</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { request } from '@/api/client'

const ENDPOINT = '/api/heatmap-share'

type Grant = Record<string, string>
type Share = Record<string, string>

const grants = ref<Grant[]>([])
const shares = ref<Share[]>([])
const stores = ref<Record<string, string>[]>([])
const errorMessage = ref('')

const grantForm = reactive({ reader_id: '', reader_name: '', grain: 'store', scope_id: '', event_id: 'EV-002' })
const accessForm = reactive({ reader_id: '', token: '' })
const accessResult = ref<any>(null)

const storeOptions = computed(() => stores.value)
const regionOptions = computed(() => {
  const seen = new Map<string, Record<string, string>>()
  for (const s of stores.value) {
    if (!seen.has(s.region_id)) seen.set(s.region_id, { region_id: s.region_id, region_name: s.region_name })
  }
  return [...seen.values()]
})

watch(() => grantForm.grain, (grain) => {
  grantForm.scope_id = grain === 'store'
    ? (storeOptions.value[0]?.store_id ?? '')
    : (regionOptions.value[0]?.region_id ?? '')
})

const stats = computed(() => [
  { label: '有效许可', value: grants.value.filter(g => g.status === '已生效').length },
  { label: '已撤回许可', value: grants.value.filter(g => g.status === '已撤回').length },
  { label: '已发布共享', value: shares.value.filter(s => s.status === '已发布').length },
  { label: '失效/撤回共享', value: shares.value.filter(s => s.status !== '已发布').length },
])

const leakCheck = computed(() => {
  if (!accessResult.value?.ok) return false
  return JSON.stringify(accessResult.value.data).includes('STORE-')
})

async function reload() {
  errorMessage.value = ''
  try {
    const [g, s, st] = await Promise.all([
      request(`${ENDPOINT}/grants`).then(r => r.json()),
      request(ENDPOINT).then(r => r.json()),
      request(`${ENDPOINT}/stores`).then(r => r.json()),
    ])
    grants.value = g.items ?? []
    shares.value = s.items ?? []
    stores.value = st.items ?? []
    if (!grantForm.scope_id && stores.value.length) {
      grantForm.scope_id = stores.value[0].store_id
    }
  } catch (e) {
    errorMessage.value = e instanceof Error ? e.message : '加载失败'
  }
}

async function submitGrant() {
  const res = await request(`${ENDPOINT}/grants`, {
    method: 'POST', body: JSON.stringify({ ...grantForm }),
  })
  const body = await res.json()
  if (!res.ok || !body.ok) errorMessage.value = body.detail?.message || body.detail || '登记失败'
  await reload()
}

async function revokeGrant(grantId: string) {
  const res = await request(`${ENDPOINT}/grants/${grantId}/revoke`, { method: 'POST' })
  if (!res.ok) errorMessage.value = '撤回失败'
  await reload()
}

async function revokeShare(shareId: string) {
  const res = await request(`${ENDPOINT}/${shareId}/revoke`, { method: 'POST' })
  if (!res.ok) errorMessage.value = '撤回失败'
  await reload()
}

async function simulateAccess() {
  accessResult.value = null
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/access/${accessForm.token}`, {
      method: 'POST', body: JSON.stringify({ reader_id: accessForm.reader_id }),
    })
    const body = await res.json()
    accessResult.value = res.ok
      ? { ok: true, data: body }
      : { ok: false, status: res.status, error: body.detail }
  } catch (e) {
    errorMessage.value = e instanceof Error ? e.message : '请求失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.section-title { font-size: 15px; margin: 18px 0 8px; }
.result-box { margin-top: 12px; padding: 12px 16px; border-radius: 8px; border: 1px solid var(--border); background: #fff; font-size: 13px; }
.result-box p { margin: 4px 0; }
.result-ok { border-left: 4px solid #22c55e; }
.result-bad { border-left: 4px solid #ef4444; }
.filter-item select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
code { background: #f0f2f5; padding: 2px 6px; border-radius: 4px; font-size: 12px; }
</style>
