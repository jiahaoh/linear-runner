import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

import { launched } from './register'
import type { Board } from './register'
// The record the runner's own test fixture gives; the runner's unit suite keeps this file current.
import fixture from './status-board.fixture'

const PANE = {
  component: 'Pane',
  requestId: 'runner-board',
  props: {
    title: 'Runner board',
    isFocused: false,
    bodyColumns: 100,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

const exact = (value: number) => ({ value, bound: 'exact' })
const unknown = { value: null, bound: null }
const none = { worker: 0, review: 0 }

// A batch under way, as `runner.py status --board` prints it: one issue done, one being validated after a
// repair, one queued. Its batch file holds the purpose and the predecessor.
const DEMO: Board = {
  schema: 'linear-runner.board/1',
  batch: {
    id: 'demo',
    phase: 'validating',
    supervisor: { status: 'running', outcome: null },
    first_launch_at: '2026-10-10T19:02:11+00:00',
    last_launch_at: '2026-10-11T01:58:48+00:00',
    stop_marker: 'left by an earlier stop',
    pending_recovery: null,
    error: null,
    lineage: { purpose: 'Build the first half.', follows: null, reason: 'The plan was agreed.', from_issues: [] },
  },
  issues: [
    {
      id: 'T-1',
      title: 'First issue',
      url: 'https://linear.app/x/issue/T-1/first-issue',
      stage: 'done',
      repairs: 0,
      blocked: none,
      summary: 'Both routes are   written\nand tested.',
      accepted_at: '2026-10-10T21:31:28+00:00',
      commit: 'abcdef0123456',
      files: ['report.html', 'plane.png'],
      elapsed_seconds: 7185.4,
      tokens: { input_tokens: exact(22402042), cached_input_tokens: exact(21964041), output_tokens: exact(115900) },
      pending_invocations: 0,
    },
    {
      id: 'T-2',
      title: 'Second issue',
      url: 'https://linear.app/x/issue/T-2/second-issue',
      stage: 'validate',
      repairs: 1,
      blocked: { worker: 2, review: 0 },
      summary: 'Both routes ran.',
      accepted_at: null,
      commit: null,
      files: [],
      elapsed_seconds: 1500,
      tokens: { input_tokens: { value: 900000, bound: 'lower-bound' }, cached_input_tokens: unknown, output_tokens: exact(4000) },
      pending_invocations: 1,
    },
    {
      id: 'T-3',
      title: 'Third issue, not claimed yet',
      url: 'https://linear.app/x/issue/T-3',
      stage: 'queued',
      repairs: 0,
      blocked: none,
      summary: null,
      accepted_at: null,
      commit: null,
      files: [],
      elapsed_seconds: null,
      tokens: { input_tokens: unknown, cached_input_tokens: unknown, output_tokens: unknown },
      pending_invocations: 0,
    },
  ],
}
// A batch whose file says nothing of itself: the lineage file is read for it.
const DEMO_B: Board = { ...DEMO, batch: { ...DEMO.batch, id: 'demo-b', lineage: {} } }

// The lineage file of the runner home, kept from before batch files held the keys. Its entry for `demo` is not
// read, since that batch's own file speaks.
const LINEAGE = {
  status: 'draft',
  batches: {
    demo: { purpose: 'An older sentence about the first half.', follows: null, reason: 'An older reason.' },
    'demo-b': {
      purpose: 'Repair what the first half left.',
      follows: 'demo',
      reason: 'The review of the first half found two defects.',
      from_issues: ['T-1'],
    },
  },
}

// What the runner prints for each batch now; a test may move it on, or put another answer in its place.
let boards: Record<string, object> = {}
let answer: { exitCode: number; stdout?: string; stderr: string } | null = null

// The host beneath the mod: a runner home with three batch files, and a runner that prints `boards`.
function host(on: On, stored: Record<string, unknown> = {}, env: Record<string, string> = {}) {
  const runs: string[][] = []
  boards = { demo: DEMO, 'demo-b': DEMO_B, fixture }
  answer = null
  mock.store(on, stored)
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/h', ...env })
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  on('fs.exists', async (_, e) => ({ value: /\/batches\/(demo(-b)?|fixture)\.json$/.test(String(e.path)) }))
  on('fs.list', async (_, e) => ({
    value: String(e.path).endsWith('/lineage') ? [{ name: 'phase.json', kind: 'file' as const, size: 1, mtimeMs: 0, isLink: false }] : [],
  }))
  on('fs.read', async (_, e) => {
    const path = String(e.path)
    if (path.endsWith('/lineage/phase.json')) return { value: JSON.stringify(LINEAGE) }

    return { value: JSON.stringify({ issues: ['T-1', 'T-2', 'T-3'] }) }
  })
  on('process.run', async (_, e) => {
    runs.push([...e.argv])
    const id = String(e.argv[e.argv.indexOf('--batch') + 1]).replace(/^.*\//, '').replace(/\.json$/, '')

    return {
      value: {
        exitCode: answer?.exitCode ?? 0,
        stdout: answer ? (answer.stdout ?? '') : JSON.stringify(boards[id]),
        stderr: answer?.stderr ?? '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })

  return Object.assign(runs, { clock })
}

const COMMAND = { command: 'runner-board', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } } as const
// What the board asks of the runner for one batch, with the paths the test gave it.
const asked = (python: string, runner: string, home: string, id: string) => [
  python, runner, 'status', '--board', '--batch', `${home}/batches/${id}.json`, '--home', home,
]

test('a name with no batch file is said so and stays off the board', async ($, on) => {
  const runs = host(on)
  const ran = await $.command.run({ ...COMMAND, args: 'add nope' })
  expect(ran.text).toContain('No batch file for nope.')
  expect(runs).toEqual([])
  const ui = await $.ui.mount({ plugin: 'runner-board', surface: 'desktop', ...PANE })
  expect(await ui.find({ key: 'batch-nope' })).toBeUndefined()
})

test('the board draws the record of the runner\'s own fixture: stages, titles, summaries, time and tokens', async ($, on) => {
  const runs = host(on)
  await $.command.run({ ...COMMAND, args: 'add fixture' })
  // One call for the batch, and nothing else: no report, no file of the board's own.
  expect(runs.length).toBe(1)
  expect(runs[0]?.slice(2)).toEqual(['status', '--board', '--batch', '/h/.config/linear-runner/batches/fixture.json', '--home', '/h/.config/linear-runner'])
  const record: Board = fixture
  const [done, active, queued] = record.issues
  if (!done || !active || !queued) throw new Error('the fixture has three issues')
  // What the test fixture is, so that a change of it is seen here: accepted, cut off in a repair, queued.
  expect(record.issues.map(issue => [issue.id, issue.stage])).toEqual([['DEV-1', 'done'], ['DEV-2', 'repair'], ['DEV-3', 'queued']])
  expect([done.elapsed_seconds, done.tokens.input_tokens.value, done.tokens.cached_input_tokens.value, done.tokens.output_tokens.value]).toEqual([210, 900, 360, 45])
  expect([active.elapsed_seconds, active.tokens.input_tokens.value, active.tokens.output_tokens.value, active.repairs]).toEqual([30, 500, 25, 1])
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'runner-board', surface, ...PANE })
    // A text the board shows that is exactly this one.
    const said = (text: string) => ui.find({ type: 'Text', text: new RegExp('^' + text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$') })
    const shows = async (text: string) => expect(await said(text), `the board does not show: ${text}`).toBeDefined()
    // The batch: its outcome, its count, its flags and the purpose its file states.
    expect((await ui.find({ key: 'batch-fixture' }))?.text).toBe('fixture  blocked  1/3  STOP marker  ▾')
    await shows(String(record.batch.lineage.purpose))
    await shows(String(record.batch.error))
    // Stages and titles, issue by issue in queue order.
    expect((await ui.find({ key: 'issue-fixture/DEV-1' }))?.text).toBe(`DEV-1  ${done.title}`)
    expect((await ui.find({ key: 'issue-fixture/DEV-2' }))?.text).toBe(`DEV-2  blocked at repair, repair 1  ${active.title}`)
    expect((await ui.find({ key: 'issue-fixture/DEV-3' }))?.text).toBe(`DEV-3  ${queued.title}`)
    expect(await ui.find({ type: 'Text', text: /1 done/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /1 stopped/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /1 queued/ })).toBeDefined()
    // Time and tokens: 210 s and 900 + 45 tokens for the first, 30 s and 500 + 25 for the second, none for the third.
    await shows('4m · 945 tok')
    await shows('1m · 525 tok')
    expect((await ui.findAll({ type: 'Text', text: / tok/ })).length).toBe(2)

    // The accepted issue: what it did, when, what it took, who blocked it, the files for the owner.
    expect(await said(String(done.summary))).toBeUndefined()
    await ui.press({ key: 'issue-fixture/DEV-1' })
    await shows(String(done.summary))
    expect((await ui.find({ type: 'Link' }))?.props.href).toBe(done.url)
    // 2026-01-01T03:00 UTC, the fixture's third moment, in the runtime's local time zone (Dec 31, 10:00 PM EST
    // in New York); the expected text is formatted here, so the test passes in any zone.
    const accepted = new Date('2026-01-01T03:00:00Z').toLocaleString('en-US', {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' })
    await shows(`${accepted}, commit 000000000`)
    await shows('4m from first start to last finish')
    await shows('900 input (360 of it read from cache), 45 output')
    await shows('blocked it once')
    for (const file of done.files) await shows('  • ' + file)
    // The active issue: its latest result, and figures that cover the phases that have ended.
    await ui.press({ key: 'issue-fixture/DEV-2' })
    await shows(String(active.summary))
    expect((await ui.find({ type: 'Link' }))?.props.href).toBe(active.url)
    await shows('1m from first start to last finish, phases ended so far')
    await shows('500 input (200 of it read from cache), 25 output')
    // The queued issue: its link, and that nothing has started.
    await ui.press({ key: 'issue-fixture/DEV-3' })
    expect((await ui.find({ type: 'Link' }))?.props.href).toBe(queued.url)
    expect(await ui.find({ type: 'Text', text: /has not claimed this issue; what it asks is in Linear/ })).toBeDefined()
    await ui.press({ key: 'issue-fixture/DEV-3' })
    await ui.unmount()
  }
})

test('a batch under way shows its step, its repairs and totals that are bounds', async ($, on) => {
  const runs = host(on)
  await $.command.run({ ...COMMAND, args: 'add demo' })
  expect(runs).toEqual([['python3', runs[0]?.[1] ?? '', 'status', '--board', '--batch', '/h/.config/linear-runner/batches/demo.json', '--home', '/h/.config/linear-runner']])
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'runner-board', surface, ...PANE })
    expect((await ui.find({ key: 'batch-demo' }))?.text).toBe('demo  running  1/3  STOP marker  ▾')
    expect((await ui.find({ key: 'issue-demo/T-1' }))?.text).toBe('T-1  First issue')
    expect((await ui.find({ key: 'issue-demo/T-2' }))?.text).toBe('T-2  validating, repair 1  Second issue')
    expect((await ui.find({ key: 'issue-demo/T-3' }))?.text).toBe('T-3  Third issue, not claimed yet')
    // The desktop draws Linear-style icons; the terminal draws their glyphs.
    expect((await ui.findAll({ type: 'Svg' })).length).toBe(surface === 'desktop' ? 4 + 7 : 0)
    expect(await ui.find({ type: 'Text', text: /1 in progress/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^2h 0m · 22\.5M tok$/ })).toBeDefined()
    // A total with a part the runner could not count in full is at least what was counted.
    expect(await ui.find({ type: 'Text', text: /^25m · ≥904K tok \+$/ })).toBeDefined()

    expect(await ui.find({ type: 'Text', text: /Both routes ran\./ })).toBeUndefined()
    await ui.press({ key: 'issue-demo/T-2' })
    expect(await ui.find({ type: 'Text', text: /^Both routes ran\.$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^≥900K input, 4K output$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^blocked 2 times$/ })).toBeDefined()
    await ui.press({ key: 'issue-demo/T-1' })
    expect(await ui.find({ type: 'Text', text: /^Both routes are written and tested\.$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /22\.4M input \(22\.0M of it read from cache\), 116K output/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /• report\.html/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /• plane\.png/ })).toBeDefined()
    await ui.press({ key: 'issue-demo/T-1' })

    // A closed card drops the STOP marker.
    await ui.press({ key: 'batch-demo' })
    expect((await ui.find({ key: 'batch-demo' }))?.text).toBe('demo  running  1/3  ▸')
    expect(await ui.find({ key: 'issue-demo/T-1' })).toBeUndefined()
    await ui.press({ key: 'batch-demo' })
    await ui.unmount()
  }
})

test('purpose and predecessor come from the batch file, and from the lineage file while a batch file has none', async ($, on) => {
  host(on)
  await $.command.run({ ...COMMAND, args: 'add demo demo-b' })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'runner-board', surface, ...PANE })
    // demo: its own file. demo-b: the lineage file. The file's older entry for demo is not read.
    expect(await ui.find({ type: 'Text', text: /^Build the first half\.$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Repair what the first half left\.$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /An older sentence/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /two defects/ })).toBeUndefined()

    await ui.press({ key: 'view-lineage' })
    expect(await ui.find({ type: 'Text', text: /not yet reviewed/ })).toBeDefined()
    if (surface === 'desktop') {
      // The desktop draws the graph: a card for each node and line, shown by a hover rule, the reason inside.
      const source = String((await ui.find({ type: 'Svg' }))?.props.source)
      expect(source).toContain('#n1:hover~#tn1')
      expect(source).toContain('#e1:hover~#te1')
      expect(source).toContain('Why demo-b followed demo')
      expect(source).toContain('The review of the first half found two')
      expect(source).toContain('>defects.<')
      expect(source).toContain('Repair what the first half left.')
      expect(source).toContain('Started: The plan was agreed.')
      expect(source).not.toContain('An older reason.')
      expect((await ui.find({ type: 'Svg' }))?.props.isInteractive).toBe(true)
      // The graph is drawn at its own size, and the two buttons change that size.
      const sized = async () => (await ui.find({ type: 'Svg' }))?.props.width
      expect(source).toContain(`width="${await sized()}"`)
      await ui.press({ key: 'zoom-in' })
      expect(await sized()).toBe(420)
      await ui.press({ key: 'zoom-out' })
      expect(await sized()).toBe(350)
      expect(await ui.find({ key: 'node-demo-b' })).toBeUndefined()
      await ui.press({ key: 'lineage-list' })
    }
    expect(await ui.find({ type: 'Text', text: /^The plan was agreed\.$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /The review of the first half found two defects\./ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /from T-1/ })).toBeDefined()
    expect((await ui.find({ key: 'node-demo-b' }))?.text).toBe('demo-b  running  1/3')

    // A node leads back to its card on the board, opened.
    await ui.press({ key: 'node-demo-b' })
    expect(await ui.find({ key: 'node-demo-b' })).toBeUndefined()
    expect(await ui.find({ key: 'issue-demo-b/T-1' })).toBeDefined()
    await ui.unmount()
  }
})

test('once every batch file states its own purpose, nothing on the board is a draft', async ($, on) => {
  host(on)
  await $.command.run({ ...COMMAND, args: 'add demo' })
  const ui = await $.ui.mount({ plugin: 'runner-board', surface: 'terminal', ...PANE })
  await ui.press({ key: 'view-lineage' })
  expect(await ui.find({ type: 'Text', text: /^The plan was agreed\.$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /not yet reviewed/ })).toBeUndefined()
})

test('the paths are the plugin\'s options', { options: { python: '~/envs/runner/bin/python', runner: '/opt/linear-runner/runner.py', home: '/data/runner-home' } }, async ($, on) => {
  // A home that is named wins over the variable, as the runner's own --home does.
  const runs = host(on, {}, { LINEAR_RUNNER_HOME: '/elsewhere' })
  await $.command.run({ ...COMMAND, args: 'add demo' })
  expect(runs).toEqual([asked('/h/envs/runner/bin/python', '/opt/linear-runner/runner.py', '/data/runner-home', 'demo')])
})

test('left alone, the options name no host: python3 of the PATH, the checkout the plugin is in, the home the runner would find', async ($, on) => {
  const runs = host(on, {}, { LINEAR_RUNNER_HOME: '/elsewhere' })
  await $.command.run({ ...COMMAND, args: 'add demo' })
  const runner = runs[0]?.[1] ?? ''
  expect(runs).toEqual([asked('python3', runner, '/elsewhere', 'demo')])
  // Two folders up from the plugin's own folder, `mods/runner-board` of a checkout, with the steps resolved.
  expect(runner).toMatch(/^\/.*\/runner\.py$/)
  expect(runner).not.toContain('..')
  expect(runner).not.toContain('/mods/')
})

test('a runner that cannot answer is said so on the card, and the last good read stays', async ($, on) => {
  const runs = host(on)
  // A runner from before `status --board`.
  answer = { exitCode: 2, stderr: 'usage: runner.py [-h]\nrunner.py: error: unrecognized arguments: --board\n' }
  await $.command.run({ ...COMMAND, args: 'add demo' })
  let ui = await $.ui.mount({ plugin: 'runner-board', surface: 'terminal', ...PANE })
  expect((await ui.find({ key: 'batch-demo' }))?.text).toBe('demo  cannot read  0/3  ▾')
  expect(await ui.find({ type: 'Text', text: /^runner\.py: error: unrecognized arguments: --board$/ })).toBeDefined()
  await ui.unmount()
  // A record of another shape is not drawn as if it were this one.
  answer = { exitCode: 0, stdout: JSON.stringify({ schema: 'linear-runner.board/2', batch: {}, issues: [] }), stderr: '' }
  await runs.clock.advance(10_000)
  ui = await $.ui.mount({ plugin: 'runner-board', surface: 'terminal', ...PANE })
  expect(await ui.find({ type: 'Text', text: /cannot read \(linear-runner\.board\/2, not linear-runner\.board\/1\)/ })).toBeDefined()
  await ui.unmount()
  // Once it reads, a later failure keeps the card and adds the problem.
  answer = null
  await runs.clock.advance(10_000)
  answer = { exitCode: 1, stderr: 'Traceback\nOSError: state.json is being written\n' }
  await runs.clock.advance(10_000)
  ui = await $.ui.mount({ plugin: 'runner-board', surface: 'terminal', ...PANE })
  expect((await ui.find({ key: 'batch-demo' }))?.text).toBe('demo  running  1/3  STOP marker  ▾')
  expect(await ui.find({ type: 'Text', text: /^OSError: state\.json is being written$/ })).toBeDefined()
})

test('the timer reads only batches under way, and stops once none is', async ($, on) => {
  const runs = host(on)
  await $.command.run({ ...COMMAND, args: 'add demo' })
  expect(runs.length).toBe(1)
  await runs.clock.advance(10_000)
  expect(runs.length).toBe(2)
  boards = { demo: { ...DEMO, batch: { ...DEMO.batch, phase: 'queue_complete', supervisor: { status: 'exited', outcome: 'complete' } } } }
  await runs.clock.advance(10_000)
  expect(runs.length).toBe(3)
  await runs.clock.advance(60_000)
  expect(runs.length).toBe(3)
})

test('batches keep the order of their first launch, whichever was launched last', async ($, on) => {
  host(on)
  boards = {
    demo: { ...DEMO, batch: { ...DEMO.batch, first_launch_at: '2026-10-10T19:02:11+00:00', last_launch_at: '2026-10-12T08:00:00+00:00' } },
    'demo-b': { ...DEMO_B, batch: { ...DEMO_B.batch, first_launch_at: '2026-10-11T09:00:00+00:00', last_launch_at: '2026-10-11T09:00:00+00:00' } },
  }
  await $.command.run({ ...COMMAND, args: 'add demo-b demo' })
  const ui = await $.ui.mount({ plugin: 'runner-board', surface: 'terminal', ...PANE })
  const cards = (await ui.findAll({ type: 'Button' })).map(button => String(button.key)).filter(key => key.startsWith('batch-'))
  expect(cards).toEqual(['batch-demo', 'batch-demo-b'])
})

test('a session with no board of its own starts from the board saved last', async ($, on) => {
  host(on, { 'batches:last': ['demo', 'gone'] })
  await $.command.run({ ...COMMAND, args: '' })
  const ui = await $.ui.mount({ plugin: 'runner-board', surface: 'desktop', ...PANE })
  expect(await ui.find({ key: 'batch-demo' })).toBeDefined()
  expect(await ui.find({ key: 'batch-gone' })).toBeUndefined()
})

test('a board kept only under an earlier session is found too', async ($, on) => {
  host(on, { 'batches:earlier-session': ['demo'], 'batches:another': [] })
  await $.command.run({ ...COMMAND, args: '' })
  const ui = await $.ui.mount({ plugin: 'runner-board', surface: 'desktop', ...PANE })
  expect(await ui.find({ key: 'batch-demo' })).toBeDefined()
})

test('only a launch names a batch for the board', () => {
  expect(launched('python3 runner.py launch --batch ~/.config/linear-runner/batches/s1-impl.json')).toEqual(['s1-impl'])
  expect(launched('cd x && python3 /r/runner.py launch --rerun-preflight --batch="b two.json" | tail')).toEqual(['b two'])
  expect(launched('python3 runner.py status --batch batches/s1-impl.json')).toEqual([])
  expect(launched('git log')).toEqual([])
  // Text a here-document feeds a program, such as a file being written, launches nothing.
  expect(launched("cat > t.ts <<'EOF'\npython3 runner.py launch --batch batches/s1-impl.json\nEOF")).toEqual([])
})

test('a launch this session ran puts its batch on the board, a command that only mentions one does not', async ($, on) => {
  host(on)
  let printed = '{"launch_id": "L-1"}'
  on('tool.call', { tool: 'Bash' }, async () => ({ result: {}, text: printed }) as never)
  const board = async () => {
    const ui = await $.ui.mount({ plugin: 'runner-board', surface: 'desktop', ...PANE })
    const card = await ui.find({ key: 'batch-demo' })
    await ui.unmount()

    return card
  }
  printed = 'README.md: python3 runner.py launch --batch batches/demo.json'
  await $.tool.call({ tool: 'Bash', command: 'grep -n "runner.py launch --batch batches/demo.json" README.md' })
  expect(await board()).toBeUndefined()
  printed = '{"launch_id": "L-1"}'
  await $.tool.call({ tool: 'Bash', command: 'python3 runner.py launch --batch batches/demo.json' })
  expect(await board()).toBeDefined()
})
