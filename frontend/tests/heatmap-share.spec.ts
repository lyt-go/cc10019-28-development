import { expect, test } from '@playwright/test'

/**
 * 授权热力共享页面 E2E（自包含，不依赖种子共享的可变状态）：
 * - beforeAll 发布本套用例专用的门店/区域共享并取得 token；
 * - 撤回用例使用独立读者，避免其他许可干扰 403 判定；
 * - 可重复执行：许可/共享重复登记会产生新行，用例按生效行动态定位。
 */
const API = '/api/heatmap-share'

test.describe('授权热力共享', () => {
  let storeToken = ''
  let regionToken = ''
  const revokeReader = 'e2e-revoke-reader'

  test.beforeAll(async ({ request }) => {
    const publish = async (grain: string, scopeId: string, issuedTo: string) => {
      const res = await request.post(API, {
        data: {
          share_name: `E2E-${grain}-${scopeId}`,
          config_id: 'BH-001',
          event_id: 'EV-002',
          grain,
          scope_id: scopeId,
          color_scale: 'viridis',
          time_range: '2026-11-20/2026-11-20',
          issued_to: issuedTo,
        },
      })
      const body = await res.json()
      return body.entry.token as string
    }
    storeToken = await publish('store', 'STORE-001', 'reader-store-001')
    regionToken = await publish('region', 'REGION-EAST', 'reader-region-east')

    // 确保种子门店读者的门店许可有效（重跑前可能已被撤回）
    const grants = await request.get(`${API}/grants?reader_id=reader-store-001`)
    const body = await grants.json()
    const hasActive = body.items.some(
      (g: any) => g.scope_id === 'STORE-001' && g.grain === 'store' && g.status === '已生效',
    )
    if (!hasActive) {
      await request.post(`${API}/grants`, {
        data: {
          reader_id: 'reader-store-001',
          reader_name: '门店账号-南京路旗舰店',
          grain: 'store',
          scope_id: 'STORE-001',
          event_id: 'EV-002',
        },
      })
    }
  })

  test.beforeEach(async ({ page }) => {
    await page.goto('/heatmap_share')
    await expect(page.locator('h2')).toHaveText('授权热力共享')
  })

  async function openLink(page: import('@playwright/test').Page, reader: string, token: string) {
    const form = page.locator('form.filter-bar').last()
    await form.locator('input').nth(0).fill(reader)
    await form.locator('input').nth(1).fill(token)
    await form.getByRole('button', { name: '打开链接' }).click()
  }

  test('许可与共享清单渲染', async ({ page }) => {
    await expect(page.getByText('HG-001')).toBeVisible()
    await expect(page.getByText('HS-001')).toBeVisible()
    await expect(page.locator('tbody tr[data-status="已生效"]').first()).toBeVisible()
  })

  test('区域读者打开区域链接：只得到区域口径且无门店泄露', async ({ page }) => {
    await openLink(page, 'reader-region-east', regionToken)

    const box = page.locator('.result-box')
    await expect(box).toBeVisible()
    await expect(box).toHaveClass(/result-ok/)
    await expect(box).toContainText('区域')
    await expect(box).toContainText('华东区')
    await expect(box).toContainText('色阶来源')
    await expect(box).toContainText('通过')
    // 结果区不应出现门店编号
    await expect(box).not.toContainText('STORE-001')
  })

  test('区域读者打开门店链接：409 授权冲突，引导改用区域链接', async ({ page }) => {
    await openLink(page, 'reader-region-east', storeToken)

    const box = page.locator('.result-box')
    await expect(box).toHaveClass(/result-bad/)
    await expect(box).toContainText('STORE_SCOPE_CONFLICT')
    await expect(box).toContainText('当前可用粒度：区域')
  })

  test('失效链接返回错误码', async ({ page }) => {
    await openLink(page, 'reader-store-001', 'not-a-real-token')
    await expect(page.locator('.result-box')).toContainText('SHARE_LINK_INVALID')
  })

  test('撤回门店许可后旧链接立即失效', async ({ page, request }) => {
    // 独立读者：仅持有一条门店许可，撤回后无其他许可，干净验证 403
    await request.post(`${API}/grants`, {
      data: {
        reader_id: revokeReader,
        reader_name: 'E2E撤回验证读者',
        grain: 'store',
        scope_id: 'STORE-001',
        event_id: 'EV-002',
      },
    })
    await page.reload()

    // 撤回前可读取门店口径
    await openLink(page, revokeReader, storeToken)
    await expect(page.locator('.result-box.result-ok')).toContainText('门店')

    // 在清单中撤回该读者当前生效的许可行
    const grantRow = page.locator('tbody tr[data-status="已生效"]', {
      hasText: revokeReader,
    }).first()
    const grantId = (await grantRow.locator('td').first().innerText()).trim()
    await grantRow.getByRole('button', { name: '撤回许可' }).click()
    // 用稳定的许可编号定位撤回后的行（行状态变化后原 data-status 定位器会失效）
    await expect(page.locator('tbody tr', { hasText: grantId }).first())
      .toContainText('已撤回')

    // 再打开同一链接：已无任何有效许可 → 403，且不返回格值
    await page.getByRole('button', { name: '打开链接' }).click()
    const box = page.locator('.result-box.result-bad')
    await expect(box).toContainText('STORE_PERMISSION_REQUIRED')
    await expect(box).not.toContainText('cells')
  })
})
