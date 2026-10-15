import type { Color, EngineInterface, PluginOptions, Register } from 'claude-code'

const PANE = 'runner-board'
const TITLE = 'Runner board'
const POLL_MS = 10_000
// The record `runner.py status --board` prints, as this board reads it.
const SCHEMA = 'linear-runner.board/1'

// Where the runner is: the plugin's options, with the defaults plugin.json gives them. A path that starts with `~/`
// is under the user's home, a bare name is a program on the PATH, and another relative path is under the plugin's
// own folder, which is `mods/runner-board` of a runner checkout: two folders up is the checkout's `runner.py`.
const DEFAULTS = { python: 'python3', runner: '../../runner.py', home: '~/.config/linear-runner' }
let config = DEFAULTS

// A total as the runner's report counts it: exact, or a bound where an attempt reported nothing.
type Count = { value: number | null; bound: string | null }
// One issue of the record. `stage` is done, deferred, queued, or the step of the active issue.
type Issue = {
  id: string
  title: string | null
  url: string | null
  stage: string
  repairs: number
  blocked: { worker: number; review: number }
  summary: string | null
  accepted_at: string | null
  commit: string | null
  files: string[]
  elapsed_seconds: number | null
  tokens: { input_tokens: Count; cached_input_tokens: Count; output_tokens: Count }
  pending_invocations: number
}
// Why a batch exists and which batch it stems from: the batch file's own keys, or, while a batch file has none,
// its entry in a file of `<runner home>/lineage/`.
type Link = {
  purpose?: string
  follows?: string | null
  reason?: string
  from_issues?: string[]
  note?: string
  // Written but not yet reviewed by the person.
  isDraft?: boolean
}
export type Board = {
  schema: string
  batch: {
    id: string
    phase: string | null
    supervisor: { status: string | null; outcome: string | null }
    first_launch_at: string | null
    last_launch_at: string | null
    stop_marker: string | null
    pending_recovery: { id?: string; kind?: string } | null
    error: string | null
    lineage: Link
  }
  issues: Issue[]
}
type Batch = {
  id: string
  issues: string[]
  board?: Board
  problem?: string
}
type Detail = { label: string; text?: string; items?: string[] }
type Kind = 'done' | 'running' | 'stopped' | 'deferred' | 'queued'
// A status icon in Linear's manner: a ring to do, a pie that fills as the work moves on, a check when done.
type Icon = {
  shape: 'ring' | 'dashed' | 'pie' | 'check' | 'pause' | 'alert' | 'live'
  hex: string
  part?: number
  isLive?: boolean
}
// `glyph` stands for the icon where no picture is drawn; `color` paints the stage's words.
type Stage = { kind: Kind; glyph: string; icon: Icon; label: string; color: Color }
type Look = { glyph: string; icon: Icon; color: Color; border: Color }

let batches: Batch[] = []
let isLoaded = false
let isRefreshing = false
let updatedAt: number | null = null
let expanded: string | null = null
// What the files of `<runner home>/lineage/` record, for the batches whose own file says nothing.
let recorded: Record<string, Link> = {}
let view: 'board' | 'lineage' = 'board'
let lineageAs: 'graph' | 'list' = 'graph'
// How large the graph is drawn, as a share of its own size.
let zoom = 1
// Batches the person opened or closed by hand; the others open unless their queue is complete.
const toggled = new Map<string, boolean>()

// A path without its `.` and `..` steps.
function tidy(path: string) {
  const steps: string[] = []
  for (const step of path.split('/')) {
    if (step === '..') steps.pop()
    else if (step !== '.' && step !== '') steps.push(step)
  }

  return '/' + steps.join('/')
}

async function places($: EngineInterface) {
  const home = (await $.env.get('HOME')) ?? ''
  const at = (path: string) => {
    if (path.startsWith('~/')) return home + path.slice(1)

    return path.startsWith('/') || !path.includes('/') ? path : tidy($.plugin.root + '/' + path)
  }
  // As the runner finds its home: the one that is named, then $LINEAR_RUNNER_HOME, then the default.
  const fromEnv = config.home === DEFAULTS.home ? await $.env.get('LINEAR_RUNNER_HOME') : undefined

  return { python: at(config.python), runner: at(config.runner), runnerHome: at(fromEnv || config.home) }
}

const LAST = 'batches:last'

async function storeKey($: EngineInterface) {
  try {
    return 'batches:' + (await $.session.id())
  } catch {
    return 'batches:default'
  }
}

async function load($: EngineInterface, id: string): Promise<Batch> {
  const { python, runner, runnerHome } = await places($)
  const file = `${runnerHome}/batches/${id}.json`
  let issues: string[] = []
  try {
    issues = (JSON.parse(await $.fs.read(file)) as { issues?: string[] }).issues ?? []
  } catch {
    return { id, issues, problem: `no batch file ${file}` }
  }
  try {
    // The runner reads the batch's state directory and the records of its issues' runs; it writes nothing.
    const ran = await $.process.run([python, runner, 'status', '--board', '--batch', file, '--home', runnerHome], {
      timeoutMs: 60_000,
    })
    if (ran.exitCode !== 0) return { id, issues, problem: ran.stderr.trim().split('\n').pop() || 'status failed' }
    const board = JSON.parse(ran.stdout) as Partial<Board>
    if (board.schema !== SCHEMA || !board.batch || !Array.isArray(board.issues)) {
      return { id, issues, problem: `the runner printed a record this board cannot read (${board.schema ?? 'no schema'}, not ${SCHEMA})` }
    }

    return { id, issues: board.issues.map(issue => issue.id), board: board as Board }
  } catch (error) {
    return { id, issues, problem: String(error) }
  }
}

async function readLineage($: EngineInterface) {
  const { runnerHome } = await places($)
  const dir = `${runnerHome}/lineage`
  const files = await $.fs.list(dir).catch(() => [])
  const next: Record<string, Link> = {}
  for (const file of files) {
    if (!file.name.endsWith('.json')) continue
    try {
      const record = JSON.parse(await $.fs.read(`${dir}/${file.name}`)) as { status?: string; batches?: Record<string, Link> }
      for (const [id, link] of Object.entries(record.batches ?? {})) next[id] = { ...link, isDraft: record.status === 'draft' }
    } catch {
      // A file that does not read leaves its batches without a recorded reason.
    }
  }
  recorded = next
}

// A batch file that holds any of the four keys speaks for itself; the lineage file is read for the others.
function linkOf(batch: Batch): Link | undefined {
  const own = batch.board?.batch.lineage

  return own && Object.keys(own).length ? own : recorded[batch.id]
}

const issueOf = (board: Board | undefined, id: string) => board?.issues.find(issue => issue.id === id)
// The first launch, so that a batch keeps its place on the board when it is launched again.
const startedAt = (batch: Batch) => batch.board?.batch.first_launch_at ?? '9'
const isLive = (batch: Batch) => !batch.board || batch.board.batch.supervisor.status === 'running'

// What of a batch the board draws: the record is all of it. A read that leaves it as it was calls for no new
// drawing, and a new drawing of the graph is a flash on the desktop, which draws it afresh each time.
const shown = (batch: Batch) => JSON.stringify([batch.issues, batch.problem, batch.board])

// With `only`, the timer's read of the batches under way: it draws again only when one of them has changed.
async function refresh($: EngineInterface, only?: (batch: Batch) => boolean) {
  if (isRefreshing || (only && !batches.some(only))) return
  isRefreshing = true
  try {
    const before = batches.map(shown).join()
    const fresh = new Map<string, Batch>()
    await Promise.all(
      batches.filter(batch => !only || only(batch)).map(async batch => fresh.set(batch.id, await load($, batch.id))),
    )
    // A failed read keeps what the last good read showed.
    batches = batches
      .map(batch => {
        const next = fresh.get(batch.id)
        if (!next) return batch

        return !next.board && batch.board ? { ...batch, problem: next.problem } : next
      })
      .sort((a, b) => startedAt(a).localeCompare(startedAt(b)))
    if (!only) await readLineage($)
    updatedAt = await $.clock.now()
    if (!only || batches.map(shown).join() !== before) $.ui.invalidate('ui.render')
  } finally {
    isRefreshing = false
  }
}

async function restore($: EngineInterface) {
  if (isLoaded) return
  isLoaded = true
  // From here on the batches under way are read again every few seconds.
  $.clock.every(POLL_MS, () => void refresh($, isLive))
  // A session with no board of its own yet starts from the board saved last: a resumed session has a new id.
  let saved = (await $.store.get(await storeKey($))) ?? (await $.store.get(LAST))
  if (!Array.isArray(saved)) {
    // Before the last board was kept under its own key, each session's was kept only under that session's.
    const keys = (await $.store.keys().catch(() => [])).filter(key => key.startsWith('batches:'))
    const boards = await Promise.all(keys.map(key => $.store.get(key)))
    saved = boards.filter(Array.isArray).sort((a, b) => b.length - a.length)[0]
  }
  if (Array.isArray(saved)) {
    // A name with no batch file behind it is not a batch: it leaves the board.
    const ids = saved.filter((id): id is string => typeof id === 'string')
    const { found } = await sift($, ids)
    batches = found.map(id => ({ id, issues: [] }))
    if (found.length !== ids.length) await save($)
  }
  await refresh($)
}

// Which of these names have a batch file.
async function sift($: EngineInterface, ids: string[]) {
  const { runnerHome } = await places($)
  const has = await Promise.all(ids.map(id => $.fs.exists(`${runnerHome}/batches/${id}.json`).catch(() => false)))

  return { found: ids.filter((_, index) => has[index]), missing: ids.filter((_, index) => !has[index]) }
}

async function save($: EngineInterface) {
  const ids = batches.map(batch => batch.id)
  await $.store.set(await storeKey($), ids)
  await $.store.set(LAST, ids)
}

// A name ending in `*` stands for every batch file whose name starts with what is before it.
async function resolve($: EngineInterface, names: string[]) {
  const { runnerHome } = await places($)
  const ids: string[] = []
  for (const name of names) {
    if (!name.endsWith('*')) {
      ids.push(name.replace(/^.*\//, '').replace(/\.json$/, ''))
      continue
    }
    const files = await $.fs.list(`${runnerHome}/batches`)
    for (const file of files) {
      if (file.name.startsWith(name.slice(0, -1)) && file.name.endsWith('.json')) ids.push(file.name.slice(0, -5))
    }
  }

  return [...new Set(ids)]
}

async function add($: EngineInterface, names: string[]) {
  await restore($)
  const known = new Set(batches.map(batch => batch.id))
  const { found, missing } = await sift($, await resolve($, names))
  const added = found.filter(id => !known.has(id))
  batches = [...batches, ...added.map(id => ({ id, issues: [] }))]
  await save($)
  await refresh($, batch => added.includes(batch.id))

  return { added, missing }
}

// The batches a shell command launches: `runner.py launch ... --batch <file>`.
// Only the command line counts: what a here-document feeds a program is text, not a launch.
export function launched(command: string) {
  const ids: string[] = []
  const line = command.split(/<<-?\s*['"]?\w/)[0] ?? ''
  for (const call of line.matchAll(/runner\.py\s+launch\b([^\n;&|]*)/g)) {
    const batch = /--batch[ =]+(?:"([^"]+)"|'([^']+)'|(\S+))/.exec(call[1] ?? '')
    const path = batch?.[1] ?? batch?.[2] ?? batch?.[3]
    if (path) ids.push(path.replace(/^.*\//, '').replace(/\.json$/, ''))
  }

  return ids
}

// Linear's status colors.
const HEX = {
  todo: '#9ca3af',
  started: '#f2c94c',
  review: '#27ae60',
  done: '#5e6ad2',
  stopped: '#f2994a',
  failed: '#eb5757',
}
const RING: Icon = { shape: 'ring', hex: HEX.todo }
const CHECK: Icon = { shape: 'check', hex: HEX.done }

// How far each step of a running issue fills its pie, and in which color.
const STEPS: Record<string, { part: number; hex: string; color: Color }> = {
  implement: { part: 0.25, hex: HEX.started, color: 'warning' },
  validate: { part: 0.5, hex: HEX.started, color: 'warning' },
  commit: { part: 0.5, hex: HEX.started, color: 'warning' },
  delivery: { part: 0.6, hex: HEX.started, color: 'warning' },
  repair: { part: 0.5, hex: HEX.stopped, color: 'warning' },
  review: { part: 0.75, hex: HEX.review, color: 'success' },
  publish: { part: 0.9, hex: HEX.review, color: 'success' },
}
const pieGlyph = (part: number) => (part < 0.34 ? '◔' : part < 0.67 ? '◑' : '◕')
const pie = (step: string, isLive = false): Icon => {
  const { part, hex } = STEPS[step] ?? { part: 0.25, hex: HEX.started }

  return { shape: 'pie', hex, part, isLive }
}
const LEGEND: [Icon, string, string][] = [
  [RING, '○', 'queued'],
  [pie('implement'), '◔', 'implement'],
  [pie('validate'), '◑', 'validate'],
  [pie('review'), '◕', 'review'],
  [CHECK, '✓', 'done'],
  [{ shape: 'pause', hex: HEX.stopped }, '⏸', 'stopped'],
  [{ shape: 'dashed', hex: HEX.todo }, '◌', 'deferred'],
]

// The 14 by 14 shapes of an icon.
function shapesOf(icon: Icon) {
  const { hex } = icon
  const ring = (more = '') => `<circle cx="7" cy="7" r="6" fill="none" stroke="${hex}" stroke-width="1.5"${more}/>`
  const bar = (x: number) => `<rect x="${x}" y="4.3" width="1.6" height="5.4" rx="0.5" fill="${hex}"/>`
  const fade = (from: string, to: string) =>
    `<animate attributeName="opacity" values="${from};${to}" dur="1.6s" repeatCount="indefinite"/>`
  const shapes: Record<Icon['shape'], string> = {
    ring: ring(),
    dashed: ring(' stroke-dasharray="1.4 1.74" stroke-dashoffset="0.65"'),
    pie:
      ring() +
      `<circle cx="7" cy="7" r="2" fill="none" stroke="${hex}" stroke-width="4" transform="rotate(-90 7 7)" ` +
      `stroke-dasharray="${(12.566 * (icon.part ?? 0.5)).toFixed(2)} 100"/>`,
    check:
      `<circle cx="7" cy="7" r="7" fill="${hex}"/>` +
      '<path d="M4 7.3 6.2 9.5 10.1 5" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    pause: ring() + bar(4.8) + bar(7.6),
    alert:
      `<circle cx="7" cy="7" r="7" fill="${hex}"/>` +
      '<rect x="6.2" y="3.2" width="1.6" height="5" rx="0.8" fill="#fff"/><circle cx="7" cy="10.3" r="1" fill="#fff"/>',
    live:
      `<circle cx="7" cy="7" r="3" fill="${hex}"/>` +
      `<circle cx="7" cy="7" r="3" fill="none" stroke="${hex}" stroke-width="1.5">` +
      `<animate attributeName="r" values="3;6.5" dur="1.6s" repeatCount="indefinite"/>${fade('0.8', '0')}</circle>`,
  }
  const pulse = icon.isLive ? fade('1;0.35', '1') : ''

  return `<g>${shapes[icon.shape]}${pulse}</g>`
}

const svg = (icon: Icon) =>
  `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14">${shapesOf(icon)}</svg>`

function stageOf(board: Board | undefined, id: string): Stage {
  const issue = issueOf(board, id)
  if (!board || !issue || issue.stage === 'queued') return { kind: 'queued', glyph: '○', icon: RING, label: 'queued', color: 'inactive' }
  if (issue.stage === 'done') return { kind: 'done', glyph: '✓', icon: CHECK, label: 'done', color: HEX.done }
  if (issue.stage === 'deferred') {
    return { kind: 'deferred', glyph: '◌', icon: { shape: 'dashed', hex: HEX.todo }, label: 'deferred', color: 'inactive' }
  }
  // Any other stage is the step the active issue is at.
  const step = issue.stage
  const repairs = issue.repairs ? `, repair ${issue.repairs}` : ''
  if (board.batch.supervisor.status === 'running') {
    const icon = pie(step, true)

    return {
      kind: 'running',
      glyph: pieGlyph(icon.part ?? 0.25),
      icon,
      label: (board.batch.phase ?? step) + repairs,
      color: STEPS[step]?.color ?? 'warning',
    }
  }
  const how = board.batch.supervisor.outcome ?? 'stopped'
  const hasFailed = Boolean(board.batch.error)

  return {
    kind: 'stopped',
    glyph: hasFailed ? '!' : '⏸',
    icon: hasFailed ? { shape: 'alert', hex: HEX.failed } : { shape: 'pause', hex: HEX.stopped },
    label: `${how} at ${step}${repairs}`,
    color: hasFailed ? 'error' : 'warning',
  }
}

function batchState(batch: Batch) {
  const about = batch.board?.batch
  if (!about) return batch.problem ? 'cannot read' : 'loading'
  if (about.supervisor.status === 'running') return 'running'
  if (about.phase === 'queue_complete') return 'complete'
  if (about.phase === 'not started') return 'not launched'

  return about.supervisor.outcome ?? about.phase ?? 'unknown'
}

function batchLook(batch: Batch, state: string): Look {
  if (state === 'cannot read' || batch.board?.batch.error) {
    return { glyph: '!', icon: { shape: 'alert', hex: HEX.failed }, color: 'error', border: HEX.failed }
  }
  if (state === 'running') return { glyph: '●', icon: { shape: 'live', hex: HEX.started }, color: 'warning', border: HEX.started }
  if (state === 'complete') return { glyph: '✓', icon: CHECK, color: HEX.done, border: 'inactive' }
  if (state === 'loading' || state === 'not launched') return { glyph: '○', icon: RING, color: 'inactive', border: 'inactive' }

  return { glyph: '⏸', icon: { shape: 'pause', hex: HEX.stopped }, color: 'warning', border: HEX.stopped }
}

const cut = (text: string, length: number) => (text.length > length ? text.slice(0, Math.max(1, length - 1)) + '…' : text)

// In the local time zone of the runtime the board runs in.
function localTime(time: string | number, zone = false) {
  try {
    return new Date(time).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      ...(zone ? { timeZoneName: 'short' as const } : {}),
    })
  } catch {
    return String(time)
  }
}

// Which batch each batch stems from, among the batches on the board.
function family(all: Batch[]) {
  const ids = new Set(all.map(batch => batch.id))
  const parentOf = (batch: Batch) => {
    const parent = linkOf(batch)?.follows

    return parent && ids.has(parent) && parent !== batch.id ? parent : null
  }
  const childrenOf = (id: string | null) => all.filter(batch => parentOf(batch) === id)

  return { parentOf, childrenOf }
}

const NODE = { w: 118, h: 34, col: 136, row: 66, pad: 14 }
const TIP = { w: 322, line: 15, chars: 44 }
type TipLine = { text: string; style?: 'b' | 'd' | 'i' }

const esc = (text: string) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

function wrap(text: string, chars: number) {
  const lines: string[] = []
  let line = ''
  for (const word of text.split(/\s+/).filter(Boolean)) {
    let rest = word
    while (rest.length > chars) {
      if (line) lines.push(line)
      lines.push(rest.slice(0, chars))
      line = ''
      rest = rest.slice(chars)
    }
    if (line && line.length + 1 + rest.length > chars) {
      lines.push(line)
      line = rest
    } else {
      line = line ? line + ' ' + rest : rest
    }
  }
  if (line) lines.push(line)

  return lines
}

// The lineage as one drawing: a node per batch under the batch it stems from, and a card that shows while the
// pointer is over a node (what the batch did) or over a line (why the batch followed). The cards are drawn last
// and shown by a rule each, since a drawing runs no script.
function lineageGraph(all: Batch[]) {
  const { parentOf, childrenOf } = family(all)
  const bare = (batch: Batch) => batch.id.replace(/-\d{8}$/, '')
  const first = all[0] ? bare(all[0]) : ''
  const prefix = first.slice(0, first.indexOf('-') + 1)
  const shared = all.length > 1 && prefix && all.every(batch => bare(batch).startsWith(prefix)) ? prefix : ''
  const short = (batch: Batch) => cut(bare(batch).slice(shared.length), 11)

  // A leaf takes the next column; a batch sits over the middle of the batches that stem from it.
  const at = new Map<string, { x: number; y: number }>()
  let leaves = 0
  let depth = 0
  const place = (batch: Batch, y: number): number => {
    at.set(batch.id, { x: 0, y })
    const below = childrenOf(batch.id)
      .filter(one => !at.has(one.id))
      .map(one => place(one, y + 1))
    const x = below.length ? below.reduce((sum, one) => sum + one, 0) / below.length : leaves++
    at.set(batch.id, { x, y })
    depth = Math.max(depth, y)

    return x
  }
  for (const batch of all) if (parentOf(batch) === null) place(batch, 0)
  for (const batch of all) if (!at.has(batch.id)) place(batch, 0)

  const width = Math.max(leaves * NODE.col + 2 * NODE.pad, TIP.w + 2 * NODE.pad)
  const left = (width - leaves * NODE.col) / 2
  const middle = (batch: Batch) => left + (at.get(batch.id)?.x ?? 0) * NODE.col + NODE.col / 2
  const top = (batch: Batch) => NODE.pad + (at.get(batch.id)?.y ?? 0) * NODE.row
  const base = 2 * NODE.pad + depth * NODE.row + NODE.h
  let height = base

  const lines: string[] = []
  const nodes: string[] = []
  const cards: string[] = []
  const rules: string[] = []
  // A card under what it explains, or over it where the drawing ends; the drawing grows if neither fits.
  const card = (id: string, owner: string, x: number, above: number, below: number, text: TipLine[]) => {
    const tall = text.length * TIP.line + 14
    const cardLeft = Math.min(Math.max(x - TIP.w / 2, 4), width - TIP.w - 4)
    const cardTop = below + 8 + tall <= base - 4 || above - 8 - tall < 4 ? below + 8 : above - 8 - tall
    height = Math.max(height, cardTop + tall + 4)
    const spans = text.map(
      (line, index) => `<tspan x="10" y="${18 + index * TIP.line}"${line.style ? ` class="${line.style}"` : ''}>${esc(line.text)}</tspan>`,
    )
    cards.push(
      `<g id="${id}" class="tip" transform="translate(${cardLeft.toFixed(1)},${cardTop.toFixed(1)})">` +
        `<rect width="${TIP.w}" height="${tall}" rx="8"/><text>${spans.join('')}</text></g>`,
    )
    rules.push(`#${owner}:hover~#${id}`)
  }

  all.forEach((batch, index) => {
    const link = linkOf(batch)
    const state = batchState(batch)
    const look = batchLook(batch, state)
    const stages = batch.issues.map(issue => stageOf(batch.board, issue))
    const done = stages.filter(stage => stage.kind === 'done').length
    const x = middle(batch)
    const y = top(batch)
    const parent = all.find(one => one.id === parentOf(batch))

    if (parent) {
      const fromY = top(parent) + NODE.h
      const half = (fromY + y) / 2
      const path = `M${middle(parent).toFixed(1)},${fromY} C${middle(parent).toFixed(1)},${half} ${x.toFixed(1)},${half} ${x.toFixed(1)},${y}`
      const kind = !link?.reason ? ' none' : link.note ? ' guess' : ''
      lines.push(
        `<g id="e${index}" class="edge"><path class="line${kind}" d="${path}"/><path class="hit" d="${path}"/>` +
          `<circle class="dot" cx="${((middle(parent) + x) / 2).toFixed(1)}" cy="${half}" r="4"/></g>`,
      )
      card(`te${index}`, `e${index}`, (middle(parent) + x) / 2, half - 6, half + 6, [
        { text: `Why ${short(batch)} followed ${short(parent)}`, style: 'b' },
        ...wrap(link?.reason ?? 'No reason is recorded.', TIP.chars).map(text => ({ text })),
        ...(link?.from_issues?.length ? wrap('from ' + link.from_issues.join(', '), TIP.chars).map(text => ({ text, style: 'd' as const })) : []),
        ...(link?.note ? wrap(link.note, TIP.chars).map(text => ({ text, style: 'i' as const })) : []),
      ])
    }

    nodes.push(
      `<g id="n${index}" class="node${state === 'running' ? ' live' : ''}" transform="translate(${(x - NODE.w / 2).toFixed(1)},${y})">` +
        `<rect width="${NODE.w}" height="${NODE.h}" rx="8"/><g transform="translate(9,10)">${shapesOf(look.icon)}</g>` +
        `<text x="30" y="21.5" class="b">${esc(short(batch))}</text>` +
        `<text x="${NODE.w - 9}" y="21.5" text-anchor="end" class="d">${done}/${batch.issues.length}</text></g>`,
    )
    const started = batch.board?.batch.first_launch_at
    const listed = batch.issues.slice(0, 10).map((issue, at) => {
      const title = issueOf(batch.board, issue)?.title
      const shown = title ? '  ' + cut(title, TIP.chars - issue.length - 4) : ''

      return { text: `${stages[at]?.glyph ?? '○'} ${issue}${shown}` }
    })
    card(`tn${index}`, `n${index}`, x, y, y + NODE.h, [
      { text: `${bare(batch)}  ${state}  ${done}/${batch.issues.length}`, style: 'b' },
      ...(started ? [{ text: localTime(started, true), style: 'd' as const }] : []),
      ...wrap(link?.purpose ?? 'No purpose is recorded.', TIP.chars).map(text => ({ text })),
      ...(!parent && link?.reason ? wrap('Started: ' + link.reason, TIP.chars).map(text => ({ text, style: 'i' as const })) : []),
      { text: '' },
      ...listed,
      ...(batch.issues.length > 10 ? [{ text: `and ${batch.issues.length - 10} more`, style: 'd' as const }] : []),
    ])
  })

  const style =
    'text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;fill:#e8e8ea}' +
    '.node rect{fill:#2b2d31;stroke:#5a5d66;stroke-width:1.2}.node.live rect{stroke:#f2c94c}' +
    '.node:hover rect{stroke:#8f97ff;stroke-width:2}' +
    '.line{fill:none;stroke:#8a8f98;stroke-width:1.5}.line.guess{stroke-dasharray:5 4}.line.none{stroke:#f2994a}' +
    '.hit{fill:none;stroke:transparent;stroke-width:16;pointer-events:stroke}.dot{fill:#2b2d31;stroke:#8a8f98;stroke-width:1.5}' +
    '.edge:hover .line,.edge:hover .dot{stroke:#8f97ff;stroke-width:2.5}' +
    '.tip{opacity:0;visibility:hidden;pointer-events:none;transition:opacity .12s}' +
    '.tip rect{fill:#17181b;stroke:#8f97ff;stroke-width:1}.b{font-weight:700}.d{fill:#9aa0aa}.i{font-style:italic;fill:#c5c8d0}' +
    (rules.length ? rules.join(',') + '{opacity:1;visibility:visible}' : '')
  const source =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">` +
    `<style>${style}</style>${lines.join('')}${nodes.join('')}${cards.join('')}</svg>`
  const steps = all.filter(batch => parentOf(batch)).map(batch => `${parentOf(batch)} to ${batch.id}`)

  return { source, width, height, alt: `Lineage of ${all.length} batches. ${steps.join('; ')}` }
}

function span(seconds: number) {
  const minutes = Math.round(seconds / 60)
  if (minutes < 1) return `${Math.round(seconds)}s`

  return minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function amount(count: number) {
  if (count >= 1e9) return (count / 1e9).toFixed(2) + 'B'
  if (count >= 1e6) return (count / 1e6).toFixed(1) + 'M'

  return count >= 1e3 ? Math.round(count / 1e3) + 'K' : String(count)
}

// How the runner marks a total that is not exact: at least, or at most.
const MARK: Record<string, string> = { exact: '', 'lower-bound': '≥', 'upper-bound': '≤' }
const isKnown = (count: Count | undefined): count is Count & { value: number } => typeof count?.value === 'number'
const tokens = (count: Count | undefined) => (isKnown(count) ? (MARK[count.bound ?? ''] ?? '≥') + amount(count.value) : 'unknown')

// The short form at the end of an issue's row: its time, and input and output tokens together; a trailing
// "+" marks an issue under way, whose figures cover the phases that have ended.
function spent(issue: Issue | undefined, isUnderWay: boolean) {
  const parts: string[] = []
  if (typeof issue?.elapsed_seconds === 'number') parts.push(span(issue.elapsed_seconds))
  const { input_tokens: input, output_tokens: output } = issue?.tokens ?? {}
  if (isKnown(input) && isKnown(output)) {
    // The sum is exact only when both parts are; a part that is a lower bound makes it one.
    const bounds = [input.bound, output.bound]
    const mark = bounds.every(bound => bound === 'exact') ? '' : bounds.includes('upper-bound') && !bounds.includes('lower-bound') ? '≤' : '≥'
    parts.push(`${mark}${amount(input.value + output.value)} tok`)
  }

  return parts.join(' · ') + (isUnderWay && parts.length ? ' +' : '')
}

// What the runner's record holds of one issue.
function details(board: Board | undefined, id: string) {
  const lines: Detail[] = []
  const issue = issueOf(board, id)
  if (!issue) return [{ label: '', text: 'The runner has no record of this issue yet.' }]
  const times = (n: number) => (n === 1 ? 'once' : `${n} times`)
  const isDone = issue.stage === 'done'
  const isWaiting = issue.stage === 'queued' || issue.stage === 'deferred'
  if (isDone) {
    const when = issue.accepted_at ? localTime(issue.accepted_at, true) : 'at an unrecorded time'
    lines.push({ label: 'Accepted', text: `${when}${issue.commit ? `, commit ${issue.commit.slice(0, 9)}` : ''}` })
  }
  if (issue.summary) {
    lines.push({ label: isDone ? 'Result' : 'Latest result', text: cut(issue.summary.replace(/\s+/g, ' '), 600) })
  }
  if (typeof issue.elapsed_seconds === 'number') {
    const sofar = !isDone && !isWaiting ? ', phases ended so far' : ''
    lines.push({ label: 'Time', text: `${span(issue.elapsed_seconds)} from first start to last finish${sofar}` })
  }
  if (isKnown(issue.tokens.input_tokens) || isKnown(issue.tokens.output_tokens)) {
    const cached = isKnown(issue.tokens.cached_input_tokens) ? ` (${tokens(issue.tokens.cached_input_tokens)} of it read from cache)` : ''
    lines.push({ label: 'Tokens', text: `${tokens(issue.tokens.input_tokens)} input${cached}, ${tokens(issue.tokens.output_tokens)} output` })
  }
  if (issue.blocked.review) lines.push({ label: 'Review', text: `blocked it ${times(issue.blocked.review)}` })
  if (issue.blocked.worker) lines.push({ label: 'Worker', text: `blocked ${times(issue.blocked.worker)}` })
  if (issue.files.length) {
    const more = issue.files.length > 12 ? [`and ${issue.files.length - 12} more`] : []
    lines.push({ label: 'Files for you', items: [...issue.files.slice(0, 12), ...more] })
  }
  if (issue.stage === 'queued') {
    lines.push({ label: 'Not started', text: 'The batch has not claimed this issue' + (issue.url ? '; what it asks is in Linear, by the link above.' : '.') })
  }
  if (!lines.length) lines.push({ label: '', text: 'No more is recorded in the batch state.' })

  return lines
}

const named = (options: PluginOptions, name: keyof typeof DEFAULTS) => {
  const value = options[name]

  return typeof value === 'string' && value.trim() ? value.trim() : DEFAULTS[name]
}

export const register: Register = (on, options) => {
  config = { python: named(options, 'python'), runner: named(options, 'runner'), home: named(options, 'home') }

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'runner-board',
      description: 'Show runner batches in a pane: add <batch id or prefix*>, remove <id>, clear',
    })
    await restore($)
    if (batches.length) void $.ui.open({ id: PANE, title: TITLE })

    return next(e)
  })

  on('command.run', { command: 'runner-board' }, async ($, e) => {
    const [verb, ...rest] = e.args.trim().split(/\s+/).filter(Boolean)
    const said: string[] = []
    await restore($)
    if (verb === 'clear') {
      batches = []
      await save($)
      said.push('Board cleared.')
    } else if (verb === 'remove') {
      const gone = new Set(await resolve($, rest))
      batches = batches.filter(batch => !gone.has(batch.id))
      await save($)
      said.push(`Removed ${gone.size} batch(es).`)
    } else if (verb) {
      const { added, missing } = await add($, verb === 'add' ? rest : [verb, ...rest])
      said.push(added.length ? `Added ${added.join(', ')}.` : 'Nothing new to add.')
      if (missing.length) said.push(`No batch file for ${missing.join(', ')}.`)
    } else {
      void refresh($)
    }
    const opened = await $.ui.open({ id: PANE, title: TITLE })
    if (!opened.isPlaced) said.push(`The pane is not shown: ${opened.reason}`)
    $.ui.invalidate('ui.render')

    return { text: said.join(' ') || 'Runner board opened.' }
  })

  // A batch this session launches joins its board; the call itself goes on untouched.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)
    try {
      // A launch that started prints its launch id; a command that only mentions one does not.
      const hasStarted = ran.deny === undefined && ran.isError !== true && Boolean(ran.text?.includes('"launch_id"'))
      const ids = hasStarted ? launched(e.command) : []
      if (ids.length) void add($, ids).catch(() => undefined)
    } catch {
      // Watching must never cost the call its result.
    }

    return ran
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Link } = $.ui.resolve(e)
    const redraw = () => $.ui.invalidate('ui.render')
    const columns = Math.max(40, e.props.bodyColumns)
    const isDesktop = e.surface === 'desktop'
    // The desktop app draws the icon; elsewhere its glyph stands for it. A live icon moves, so it is drawn in a frame.
    const mark = (icon: Icon, glyph: string, color: Color, alt: string) =>
      e.surface === 'desktop' ? (
        $.ui.resolve(e).Svg({
          source: svg(icon),
          alt,
          width: 14,
          height: 14,
          ...(icon.isLive || icon.shape === 'live' ? { isInteractive: true } : {}),
        })
      ) : (
        <Text color={color}>{glyph}</Text>
      )
    const all = batches.flatMap(batch => batch.issues.map(issue => stageOf(batch.board, issue)))
    const tally = (kind: Kind) => all.filter(stage => stage.kind === kind).length
    const totals: [Kind, string, Color][] = [
      ['done', 'done', HEX.done],
      ['running', 'in progress', 'warning'],
      ['stopped', 'stopped', 'warning'],
      ['deferred', 'deferred', 'inactive'],
      ['queued', 'queued', 'inactive'],
    ]

    const tab = (name: typeof view, label: string, hotkey: string) => (
      <Button
        key={'view-' + name}
        label={label}
        hotkey={hotkey}
        variant={view === name ? 'primary' : 'secondary'}
        onPress={() => {
          view = name
          redraw()
        }}
      />
    )

    // The lineage view: each batch under the batch it stems from, the reason written on the way down.
    const { parentOf, childrenOf } = family(batches)
    // A drawing past the size a surface takes gives way to the list.
    const drawn = view === 'lineage' && lineageAs === 'graph' && batches.length ? lineageGraph(batches) : null
    const graph =
      drawn && drawn.source.length <= 131_072 && e.surface === 'desktop'
        ? // Its size is given outright: a drawing in a frame is not sized by its own markup.
          $.ui.resolve(e).Svg({
            source: drawn.source,
            alt: drawn.alt,
            width: Math.round(drawn.width * zoom),
            height: Math.round(drawn.height * zoom),
            isInteractive: true,
          })
        : null
    const shape = (name: typeof lineageAs, label: string) => (
      <Button
        key={'lineage-' + name}
        label={label}
        variant={lineageAs === name ? 'primary' : 'secondary'}
        onPress={() => {
          lineageAs = name
          redraw()
        }}
      />
    )
    const node = (batch: Batch) => {
      const state = batchState(batch)
      const look = batchLook(batch, state)
      const done = batch.issues.filter(issue => stageOf(batch.board, issue).kind === 'done').length
      const started = batch.board?.batch.first_launch_at

      return (
        <Box flexDirection="column">
          <Box flexDirection="row" alignItems="center" columnGap={1}>
            {mark(look.icon, look.glyph, look.color, state)}
            <Box flexGrow={1}>
              <Button
                key={'node-' + batch.id}
                plain
                onPress={() => {
                  view = 'board'
                  toggled.set(batch.id, true)
                  redraw()
                }}
              >
                <Text bold>{batch.id.replace(/-\d{8}$/, '')}</Text>
                {state !== 'complete' && <Text color={look.color}>{'  ' + state}</Text>}
                <Text dimColor>{'  ' + done + '/' + batch.issues.length}</Text>
              </Button>
            </Box>
            {started && <Text dimColor>{localTime(started)}</Text>}
          </Box>
          <Box paddingLeft={3}>
            <Text dimColor wrap="wrap">
              {linkOf(batch)?.purpose ?? 'No purpose recorded.'}
            </Text>
          </Box>
        </Box>
      )
    }
    const why = (batch: Batch) => {
      const link = linkOf(batch)
      if (!link?.reason) return <Text color="warning">↓ No reason recorded for this batch.</Text>

      return (
        <Box flexDirection="column">
          <Text wrap="wrap">
            <Text color="suggestion" bold>
              {parentOf(batch) ? '↓ ' : '● '}
            </Text>
            <Text italic>{link.reason}</Text>
          </Text>
          {link.from_issues?.length ? <Text dimColor>{'   from ' + link.from_issues.join(', ')}</Text> : null}
          {link.note && (
            <Text dimColor italic wrap="wrap">
              {'   ' + link.note}
            </Text>
          )}
        </Box>
      )
    }
    // A batch with one successor continues straight down; where several stem from it, each gets its own branch.
    const seen = new Set<string>()
    const chain = (batch: Batch): JSX.Element[] => {
      if (seen.has(batch.id)) return []
      seen.add(batch.id)
      const next = childrenOf(batch.id)
      const [only] = next
      if (next.length === 1 && only) return [node(batch), why(only), ...chain(only)]

      return [
        node(batch),
        ...next.map(one => (
          <Box
            flexDirection="column"
            marginLeft={1}
            paddingLeft={1}
            rowGap={isDesktop ? 1 : 0}
            borderStyle="quote"
            borderColor="inactive"
            borderDimColor
          >
            {why(one)}
            {chain(one)}
          </Box>
        )),
      ]
    }

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between" alignItems="center">
          <Box flexDirection="row" columnGap={1}>
            {tab('board', 'Board', 'b')}
            {tab('lineage', 'Lineage', 'l')}
          </Box>
          <Button
            key="refresh"
            label="Refresh"
            hotkey="r"
            onPress={() => void refresh($)}
          />
        </Box>
        <Text>
          <Text bold>{batches.length} batches</Text>
          <Text dimColor>{'  ·  ' + all.length + ' issues'}</Text>
          {totals
            .filter(([kind]) => tally(kind) > 0)
            .map(([kind, word, color]) => (
              <Text color={color}>{'  ·  ' + tally(kind) + ' ' + word}</Text>
            ))}
          {updatedAt !== null && <Text dimColor>{'  ·  read ' + localTime(updatedAt, true)}</Text>}
        </Text>
        {batches.length === 0 && (
          <Text dimColor>No batch here yet. Add one with /runner-board add &lt;batch id&gt;; a trailing * adds several.</Text>
        )}
        {view === 'lineage' && (
          <Box flexDirection="column" marginTop={1} rowGap={isDesktop ? 1 : 0}>
            {batches.some(batch => linkOf(batch)?.isDraft) && (
              <Text color="warning" wrap="wrap">
                Draft: purposes and reasons from a lineage file that is not yet reviewed.
              </Text>
            )}
            {isDesktop && (
              <Box flexDirection="row" alignItems="center" columnGap={1}>
                {shape('graph', 'Graph')}
                {shape('list', 'List')}
                {graph && (
                  <Button
                    key="zoom-out"
                    label="−"
                    onPress={() => {
                      zoom = Math.max(0.6, Math.round((zoom - 0.2) * 10) / 10)
                      redraw()
                    }}
                  />
                )}
                {graph && (
                  <Button
                    key="zoom-in"
                    label="+"
                    onPress={() => {
                      zoom = Math.min(2.4, Math.round((zoom + 0.2) * 10) / 10)
                      redraw()
                    }}
                  />
                )}
                {graph && <Text dimColor>{Math.round(zoom * 100)}% · hover a batch or a line</Text>}
              </Box>
            )}
            {graph ?? childrenOf(null).flatMap(root => [why(root), ...chain(root)])}
          </Box>
        )}
        <Box flexDirection="column" marginTop={1} rowGap={isDesktop ? 1 : 0} display={view === 'board' ? 'flex' : 'none'}>
          {batches.map(batch => {
            const about = batch.board?.batch
            const state = batchState(batch)
            const look = batchLook(batch, state)
            const stages = batch.issues.map(issue => stageOf(batch.board, issue))
            const done = stages.filter(stage => stage.kind === 'done').length
            const isOpen = toggled.get(batch.id) ?? state !== 'complete'
            const isQuiet = state === 'complete'
            const name = batch.id.replace(/-\d{8}$/, '')
            const started = about?.first_launch_at
            const purpose = linkOf(batch)?.purpose
            const flags = [about?.stop_marker ? 'STOP marker' : '', about?.pending_recovery ? 'recovery pending' : '']
              .filter(Boolean)
              .join(' · ')

            return (
              <Box
                key={batch.id}
                flexDirection="column"
                paddingX={1}
                borderStyle="round"
                borderColor={look.border}
                borderDimColor={isQuiet}
                hover={{ borderColor: 'text', borderDimColor: false }}
                {...(state === 'running' && isDesktop ? { backgroundColor: HEX.started + '14' } : {})}
              >
                <Box flexDirection="row" alignItems="center" columnGap={1}>
                  {mark(look.icon, look.glyph, look.color, state)}
                  <Box flexGrow={1}>
                    <Button
                      key={'batch-' + batch.id}
                      plain
                      onPress={() => {
                        toggled.set(batch.id, !isOpen)
                        redraw()
                      }}
                    >
                      <Text bold={!isQuiet}>{name}</Text>
                      {!isQuiet && (
                        <Text color={look.color} bold>
                          {'  ' + state}
                        </Text>
                      )}
                      <Text dimColor>{'  ' + done + '/' + batch.issues.length}</Text>
                      {isOpen && !isQuiet && flags && <Text color="warning">{'  ' + flags}</Text>}
                      <Text dimColor>{isOpen ? '  ▾' : '  ▸'}</Text>
                    </Button>
                  </Box>
                  {started && <Text dimColor>{localTime(started)}</Text>}
                </Box>
                {purpose && (
                  <Box paddingLeft={3}>
                    <Text dimColor wrap="wrap">
                      {purpose}
                    </Text>
                  </Box>
                )}
                {isOpen && batch.problem && <Text color="error">{cut(batch.problem, 300)}</Text>}
                {isOpen && about?.error && <Text color="error">{cut(String(about.error), 300)}</Text>}
                {isOpen && batch.issues.length > 0 && (
                  // A bar down the left with a dash to each issue: the batch's issues as a tree.
                  <Box
                    flexDirection="column"
                    marginLeft={isDesktop ? 1 : 0}
                    borderStyle="quote"
                    borderColor="inactive"
                    borderDimColor
                  >
                    {batch.issues.map((issue, index) => {
                      const stage = stages[index] ?? stageOf(batch.board, issue)
                      const held = issueOf(batch.board, issue)
                      const key = batch.id + '/' + issue
                      const isPlain = stage.kind === 'done' || stage.kind === 'queued'
                      const cost = spent(held, stage.kind === 'running')
                      const used = issue.length + (isPlain ? 0 : stage.label.length + 2) + cost.length + 12
                      const title = held?.title ? cut(held.title, Math.max(16, columns - used)) : ''

                      return (
                        <Box key={key} flexDirection="column">
                          <Box flexDirection="row" alignItems="center" columnGap={1}>
                            <Text color="inactive">─</Text>
                            {mark(stage.icon, stage.glyph, stage.color, stage.label)}
                            <Box flexGrow={1}>
                              <Button
                                key={'issue-' + key}
                                plain
                                onPress={() => {
                                  expanded = expanded === key ? null : key
                                  redraw()
                                }}
                              >
                                <Text bold={!isPlain} dimColor={stage.kind === 'queued'}>
                                  {issue}
                                </Text>
                                {!isPlain && <Text color={stage.color}>{'  ' + stage.label}</Text>}
                                {title && <Text dimColor>{'  ' + title}</Text>}
                              </Button>
                            </Box>
                            {cost && <Text dimColor>{cost}</Text>}
                          </Box>
                          {expanded === key && (
                            <Box
                              flexDirection="column"
                              marginLeft={4}
                              marginBottom={1}
                              paddingLeft={1}
                              borderStyle="quote"
                              borderColor={stage.icon.hex}
                            >
                              {held?.url && <Link href={held.url} label={held.title ?? `${issue} in Linear`} />}
                              {details(batch.board, issue).map(line => (
                                <Box flexDirection="column">
                                  <Text wrap="wrap">
                                    {line.label && <Text bold>{line.label + '  '}</Text>}
                                    {line.text && <Text dimColor>{line.text}</Text>}
                                  </Text>
                                  {line.items?.map(item => (
                                    <Text dimColor wrap="wrap">
                                      {'  • ' + item}
                                    </Text>
                                  ))}
                                </Box>
                              ))}
                            </Box>
                          )}
                        </Box>
                      )
                    })}
                  </Box>
                )}
              </Box>
            )
          })}
        </Box>
        {all.length > 0 && (
          <Box flexDirection="row" flexWrap="wrap" columnGap={2} marginTop={1}>
            {LEGEND.map(([icon, glyph, word]) => (
              <Box flexDirection="row" alignItems="center" columnGap={1}>
                {mark(icon, glyph, icon.hex, word)}
                <Text dimColor>{word}</Text>
              </Box>
            ))}
          </Box>
        )}
      </Box>
    )
  })
}
