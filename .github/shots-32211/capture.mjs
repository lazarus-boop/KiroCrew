/**
 * Throwaway screenshot script for .github/workflows/shots-32211.yml (KiroCrew PR #32211).
 *
 * Drives Playwright Chromium against a REAL gateway the workflow booted on a host
 * whose user.max_user_namespaces is 0. Nothing here stubs an API: the page talks
 * to the gateway, and the gateway's own probe produces the cap-zero verdict.
 *
 *   node capture.mjs gate <gateway-state.json>   first-run gate, light and dark
 *   node capture.mjs crew <gateway-state.json>   Settings > Remote Crew AWS check
 *
 * Env: WEBSITE_DIR (for the installed Playwright), SHOTS_OUT, MEASUREMENTS.
 * Each shot runs in its own try/catch and its own browser context, so one failing
 * shot never stops the next. Every assertion lands in measurements.json "checks";
 * the workflow's verdict step fails the job on any failed one.
 */
import { createRequire } from 'node:module'
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const WEBSITE = resolve(process.env.WEBSITE_DIR || 'website')
const require = createRequire(join(WEBSITE, 'package.json'))
let chromium
try {
  ;({ chromium } = require('playwright'))
} catch {
  ;({ chromium } = require('@playwright/test'))
}

const OUT = resolve(process.env.SHOTS_OUT || 'shots')
const MEASUREMENTS = resolve(process.env.MEASUREMENTS || join(OUT, 'measurements.json'))
const PHASE = process.argv[2]
const STATE = JSON.parse(readFileSync(process.argv[3], 'utf-8'))
const BASE = STATE.base_url
mkdirSync(OUT, { recursive: true })

// The window: wide enough for the gate's two-column panel (max-w-6xl = 1152 px)
// and tall enough that the panel reaches its 760 px cap (100vh - 48 px).
const VIEWPORT = { width: 1440, height: 900 }

const GATE_HEADING = 'Kiro CLI is installed but could not be verified'
const SYSCTL_CMD = 'sudo sysctl -w user.max_user_namespaces=15000'
const PERSIST_CMD = "echo 'user.max_user_namespaces=15000' | sudo tee /etc/sysctl.d/99-userns.conf"
const FORBIDDEN = /temporary|try again|retry/i

// ── measurements.json (shared with gateway.py; steps run one at a time) ──

function load() {
  if (existsSync(MEASUREMENTS)) return JSON.parse(readFileSync(MEASUREMENTS, 'utf-8'))
  return { schema: 1, pr: 32211, checks: [] }
}

function save(data) {
  const tmp = `${MEASUREMENTS}.tmp`
  writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`)
  renameSync(tmp, MEASUREMENTS)
}

function store(key, value) {
  const data = load()
  const parts = key.split('.')
  let node = data
  for (const part of parts.slice(0, -1)) node = node[part] ??= {}
  node[parts.at(-1)] = value
  save(data)
}

let failures = 0
function check(name, ok, detail) {
  const data = load()
  data.checks ??= []
  data.checks.push({ name, ok: !!ok, detail })
  save(data)
  if (!ok) failures += 1
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}: ${JSON.stringify(detail).slice(0, 400)}`)
  return !!ok
}

// ── shared steps ──

async function newPage(browser, theme) {
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    locale: 'en-US',
    colorScheme: theme,
  })
  const page = await context.newPage()
  const problems = []
  page.on('pageerror', err => problems.push(`pageerror: ${String(err).slice(0, 300)}`))
  page.on('console', msg => {
    if (msg.type() === 'error') problems.push(`console: ${msg.text().slice(0, 300)}`)
  })
  return { context, page, problems }
}

/** Sign in the way the READY line's link does, falling back to the exchanged cookie. */
async function signIn(context, page) {
  await page.goto(`${BASE}/?token=${encodeURIComponent(STATE.token)}`, { waitUntil: 'domcontentloaded' })
  const cookies = await context.cookies(BASE)
  if (cookies.some(c => c.name.startsWith('mc_token_'))) return 'link token (/?token=<READY token>)'
  // The link token is valid for a few minutes; the helper exchanged it for the
  // session cookie right after READY, which is the same sign-in.
  const [name, ...rest] = STATE.cookie.split('=')
  await context.addCookies([{ name, value: rest.join('='), url: BASE }])
  return 'session cookie the helper got from the same /?token= exchange'
}

/** Seed the theme and first-run completion through the dashboard's own API. */
async function seedTheme(page, theme) {
  const body = { mode: theme, onboarded: true, import_onboarded: true, privacy_acked: true, crewmates_onboarded: true }
  const response = await page.request.put(`${BASE}/api/config/theme`, {
    data: body,
    headers: { Origin: BASE },
  })
  return { request: 'PUT /api/config/theme', body, status: response.status() }
}

async function waitForMode(page, theme) {
  try {
    await page.waitForFunction(m => document.documentElement.dataset.mode === m, theme, { timeout: 15000 })
  } catch {
    // Recorded below; the shot still runs so the wrong theme is visible.
  }
  return page.evaluate(() => ({
    mode: document.documentElement.dataset.mode ?? null,
    theme: document.documentElement.dataset.theme ?? null,
  }))
}

async function settle(page) {
  await page.evaluate(() => document.fonts.ready)
  await page.waitForTimeout(800)
}

/** waitForResponse, pre-handled: a wait abandoned by an earlier failure must not
 *  surface later as an unhandled rejection, which ends the process (and with it
 *  the next shot). Awaiting the returned promise still throws on a timeout. */
function responseWaiter(page, predicate) {
  const waiter = page.waitForResponse(predicate, { timeout: 60000 })
  waiter.catch(() => {})
  return waiter
}

function inside(box) {
  return !!box
    && box.x >= 0
    && box.y >= 0
    && box.x + box.width <= VIEWPORT.width + 0.5
    && box.y + box.height <= VIEWPORT.height + 0.5
}

// ── the first-run gate ──

async function shootGate(browser, theme, { recheck }) {
  const name = `linux-gate-cap-zero-${theme}`
  const result = { file: `${name}.png`, theme, viewport: VIEWPORT, device_scale_factor: 2 }
  const { context, page, problems } = await newPage(browser, theme)
  try {
    result.signed_in_via = await signIn(context, page)
    result.seeded = await seedTheme(page, theme)
    // A user opens the dashboard root; the gate replaces the app while first-run
    // setup cannot verify Kiro CLI.
    const statusResponse = responseWaiter(page, r => r.url().includes('/api/kiro-prerequisite'))
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
    const firstStatus = await statusResponse
    result.prerequisite_request = { url: firstStatus.url().replace(BASE, ''), status: firstStatus.status() }
    const heading = page.getByRole('heading', { name: GATE_HEADING })
    await heading.waitFor({ state: 'visible', timeout: 60000 })
    result.resolved = await waitForMode(page, theme)
    await settle(page)

    const surface = page.locator('main').filter({ has: heading }).first()
    const panel = surface.locator('xpath=./div').first()
    const panelBox = await panel.boundingBox()
    const scroll = await surface.getByTestId('gate-scroll-region').evaluate(el => ({
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
    }))
    const text = await surface.innerText()
    // The alert icon wrapper, scoped to the gate's own column so an icon in the
    // shell's aside can never answer for it.
    const tone = await surface.getByTestId('gate-scroll-region').locator('.h-11.w-11').first()
      .evaluate(el => [...el.classList])
    const commands = {}
    for (const cmd of [SYSCTL_CMD, PERSIST_CMD]) {
      const code = surface.locator('code', { hasText: cmd }).first()
      commands[cmd] = (await code.count())
        ? await code.evaluate(el => ({ text: el.textContent, in_copy_button: !!el.closest('button') }))
        : null
    }
    Object.assign(result, { panel_box: panelBox, scroll_region: scroll, tone_classes: tone, commands, visible_text: text })

    await page.screenshot({ path: join(OUT, result.file) })
    result.saved = true

    check(`${name}: theme resolved to ${theme}`, result.resolved.mode === theme, result.resolved)
    check(`${name}: gate panel is inside the viewport`, inside(panelBox), { panelBox, viewport: VIEWPORT })
    check(`${name}: gate content is not clipped by its scroll region`, scroll.scrollHeight <= scroll.clientHeight + 1, scroll)
    check(`${name}: text has no temporary/try again/retry wording`, !FORBIDDEN.test(text), text.match(FORBIDDEN)?.[0] ?? null)
    check(`${name}: text names user.max_user_namespaces`, text.includes('user.max_user_namespaces'), null)
    check(`${name}: shows the cap-zero body`, text.includes('User namespaces are disabled on this host'), null)
    check(`${name}: "How to fix" heading, not "If this keeps happening"`,
      /how to fix/i.test(text) && !/if this keeps happening/i.test(text), null)
    check(`${name}: danger tone on the alert icon`,
      tone.includes('bg-danger/10') && !tone.includes('bg-accent-subtle'), tone)
    for (const cmd of [SYSCTL_CMD, PERSIST_CMD]) {
      check(`${name}: copyable command ${cmd}`, commands[cmd]?.text === cmd && commands[cmd]?.in_copy_button, commands[cmd])
    }

    if (recheck) {
      // The user's Check again: an explicit re-probe. The ENOSPC verdict is never
      // cached, so this runs the probe again and must name the cap again.
      const recheckResponse = responseWaiter(
        page,
        r => r.url().includes('/api/kiro-prerequisite') && r.url().includes('refresh=explicit'),
      )
      await surface.getByRole('button', { name: 'Check again' }).click()
      const response = await recheckResponse
      const payload = await response.json()
      await settle(page)
      const after = await surface.innerText()
      result.check_again = {
        status: response.status(),
        sandbox_remedy: payload.sandbox_remedy,
        sandbox_failure_kind: payload.sandbox_failure_kind,
        sandbox_detail: payload.sandbox_detail,
        still_shows_cap_zero_body: after.includes('User namespaces are disabled on this host'),
      }
      check(`${name}: Check again re-probes and still reports userns_cap_zero`,
        payload.sandbox_remedy === 'userns_cap_zero' && result.check_again.still_shows_cap_zero_body,
        result.check_again)
    }
  } catch (err) {
    result.error = String(err?.stack || err).slice(0, 2000)
    check(`${name}: shot completed`, false, result.error.split('\n')[0])
    if (!result.saved) {
      await page.screenshot({ path: join(OUT, `${name}.failed.png`) }).catch(() => {})
    }
  } finally {
    result.page_problems = problems.slice(0, 20)
    store(`shots.${name}`, result)
    await context.close()
  }
}

// ── Settings > Remote Crew > Set up a new one ──

async function shootCrew(browser, theme) {
  const name = theme === 'light' ? 'linux-remote-crew-cap-zero' : `linux-remote-crew-cap-zero-${theme}`
  const result = { file: `${name}.png`, theme, viewport: VIEWPORT, device_scale_factor: 2 }
  const { context, page, problems } = await newPage(browser, theme)
  try {
    result.signed_in_via = await signIn(context, page)
    result.seeded = await seedTheme(page, theme)
    await page.goto(`${BASE}/settings`, { waitUntil: 'domcontentloaded' })
    // The Settings sidebar entry a user clicks. If no element with that name
    // answers, the tab's own URL is the fallback, and the record says so.
    const entry = page.getByRole('link', { name: /^Remote Crew\b/ })
      .or(page.getByRole('button', { name: /^Remote Crew\b/ }))
      .or(page.getByRole('tab', { name: /^Remote Crew\b/ }))
      .first()
    try {
      await entry.waitFor({ state: 'visible', timeout: 20000 })
      await entry.click()
      result.reached_by = 'Settings sidebar: Remote Crew'
    } catch {
      await page.goto(`${BASE}/settings/instances`, { waitUntil: 'domcontentloaded' })
      result.reached_by = 'URL /settings/instances (no clickable "Remote Crew" entry found)'
    }
    // The setup tab fires the AWS preflight on open; the response is the
    // gateway's own 503 for a refused sandbox.
    const preflight = responseWaiter(page, r => r.url().includes('/api/cloud/preflight'))
    const setupTab = page.getByRole('radio', { name: 'Set up a new one' })
      .or(page.getByRole('button', { name: 'Set up a new one' }))
      .first()
    await setupTab.waitFor({ state: 'visible', timeout: 30000 })
    await setupTab.click()
    const response = await preflight
    let body = null
    try { body = await response.json() } catch { body = (await response.text()).slice(0, 2000) }
    result.preflight = { url: response.url().replace(BASE, ''), status: response.status(), body }

    const notice = page.getByRole('alert').filter({ hasText: 'user namespaces are disabled on this host' }).first()
    await notice.waitFor({ state: 'visible', timeout: 60000 })
    result.resolved = await waitForMode(page, theme)
    await notice.scrollIntoViewIfNeeded()
    await settle(page)
    const box = await notice.boundingBox()
    const text = await notice.innerText()
    Object.assign(result, { notice_box: box, visible_text: text })

    await page.screenshot({ path: join(OUT, result.file) })
    result.saved = true

    check(`${name}: theme resolved to ${theme}`, result.resolved.mode === theme, result.resolved)
    check(`${name}: preflight is the 503 sandbox refusal with remedy userns_cap_zero`,
      response.status() === 503 && body?.code === 'sandbox_unavailable' && body?.remedy === 'userns_cap_zero',
      { status: response.status(), code: body?.code, kind: body?.kind, remedy: body?.remedy })
    check(`${name}: refusal notice is inside the viewport`, inside(box), { box, viewport: VIEWPORT })
    check(`${name}: notice names user.max_user_namespaces and says credentials were not checked`,
      text.includes('user.max_user_namespaces') && text.includes('credentials were not checked'), null)
    check(`${name}: notice has no temporary/try again/in a moment wording`,
      !/temporary|try again|in a moment/i.test(text), text.match(/temporary|try again|in a moment/i)?.[0] ?? null)
  } catch (err) {
    result.error = String(err?.stack || err).slice(0, 2000)
    check(`${name}: shot completed`, false, result.error.split('\n')[0])
    if (!result.saved) {
      await page.screenshot({ path: join(OUT, `${name}.failed.png`) }).catch(() => {})
    }
  } finally {
    result.page_problems = problems.slice(0, 20)
    store(`shots.${name}`, result)
    await context.close()
  }
}

// Playwright's Chromium launches with --no-sandbox by default (chromiumSandbox
// false), so a host with user namespaces disabled still runs it.
process.on('unhandledRejection', err => {
  console.log(`unhandled rejection (ignored, the shot that owned it already failed): ${String(err).slice(0, 300)}`)
})
const { LD_LIBRARY_PATH: _drop, ...env } = process.env
const browser = await chromium.launch({ env })
try {
  if (PHASE === 'gate') {
    await shootGate(browser, 'light', { recheck: true })
    await shootGate(browser, 'dark', { recheck: false })
  } else if (PHASE === 'crew') {
    await shootCrew(browser, 'light')
    await shootCrew(browser, 'dark')
  } else {
    throw new Error(`unknown phase ${PHASE}; expected gate or crew`)
  }
} finally {
  await browser.close()
}
process.exit(failures ? 1 : 0)
