import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

// Exercise the actual effects with controlled network/EventSource timing. No DOM
// is needed: these regressions concern requests racing with build retirement.
function component(path, api) {
  const effects = []
  const state = []
  const streams = []
  const timers = []
  const hooks = {
    useState(initial) {
      const index = state.length
      state.push(initial)
      return [initial, value => { state[index] = typeof value === 'function' ? value(state[index]) : value }]
    },
    useRef: current => ({ current }),
    useCallback: fn => fn,
    useEffect: fn => effects.push(fn),
  }
  class Source {
    constructor() { this.handlers = {}; this.closed = false; streams.push(this) }
    addEventListener(name, fn) { this.handlers[name] = fn }
    close() { this.closed = true }
  }
  const exports = {}
  const source = readFileSync(new URL(path, import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText
  vm.runInNewContext(compiled, {
    exports, AbortController, EventSource: Source, Error,
    window: { setTimeout: (...args) => timers.push(args), clearTimeout() {} },
    require(name) {
      if (name === 'react') return hooks
      if (name === 'react/jsx-runtime') return { jsx: (...args) => args, jsxs: (...args) => args }
      if (name.endsWith('/api/client')) return api
      return {}
    },
  })
  return { exports, effects, state, streams, timers }
}
const flush = () => new Promise(resolve => setImmediate(resolve))

test('build retirement during events reports missing history without an unhandled rejection', async () => {
  let reads = 0
  const fixture = component('../src/routes/BuildPage.tsx', {
    getBuild: async () => {
      if (reads++) throw new Error('Build not found')
      return { build_id: 'id', state: 'success' }
    },
    getArtifacts: async () => [],
  })
  fixture.exports.BuildPage({ buildId: 'id' })
  const dispose = fixture.effects[0]()
  await flush()
  fixture.streams[0].handlers.end({ data: '{"state":"deleted"}' })
  await flush()
  assert.equal(fixture.state[2], 'Build not found')
  assert.equal(fixture.streams[0].closed, true)
  dispose()
})

test('log retirement between snapshot and stream does not reconnect forever', async () => {
  let reads = 0
  const fixture = component('../src/components/LogViewer.tsx', {
    getLog: async () => {
      if (reads++) throw new Error('Log not found')
      return { content: 'done', offset: 4, state: 'success' }
    },
  })
  fixture.exports.LogViewer({ buildId: 'id', job: 'test' })
  const dispose = fixture.effects[0]()
  await flush()
  fixture.streams[0].onerror()
  await flush()
  assert.equal(fixture.state[1], 'Log not found')
  assert.equal(fixture.streams[0].closed, true)
  assert.equal(fixture.timers.length, 0)
  dispose()
})
