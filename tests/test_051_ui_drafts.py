"""Actual state-transition tests for the 0.5.1 Desktop settings editor.

These drive the real ``desktop/plugin.js`` through a small React-compatible
harness. They exercise draft revision capture, server refetch while a draft is
open, stale-revision rejection, and raw comma-separated list editing rather
than asserting on source text.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


SDK_STUB = r"""
const el = (type, props = {}) => ({ type, props })
export const host = {
  notify() {},
  notifyError() {},
  composer: { insertText: async () => true, focus() {} }
}
export const Badge = ({ children }) => el('badge', { children })
export const Button = ({ children, onClick, variant, loading, disabled }) =>
  el('button', { children, onClick, variant, loading, disabled })
export const ConfirmDialog = ({ open, title, description, children, onConfirm, onClose, confirmLabel }) =>
  open
    ? el('dialog', {
        children: [
          title,
          description,
          children,
          el('button', { children: confirmLabel || 'Confirm', onClick: onConfirm }),
          el('button', { children: 'Cancel', onClick: onClose })
        ]
      })
    : null
export const Input = props => el('input', props)
export const ListRow = ({ title, description, action }) => el('listrow', { children: [title, description, action] })
export const StatusDot = ({ tone }) => el('statusdot', { tone })
export const Switch = ({ checked, onCheckedChange, ...props }) => el('switch', { ...props, checked, onCheckedChange })
export const useQuery = options =>
  options && Array.isArray(options.queryKey) && options.queryKey[1] === 'settings'
    ? globalThis.__settings
    : globalThis.__status
export const useQueryClient = () => ({ invalidateQueries() {} })
"""

REACT_STUB = r"""
const slots = new Map()
let current = { name: 'root', index: 0 }

export function useState(initial) {
  const key = current.name + ':' + current.index++
  if (!slots.has(key)) {
    slots.set(key, typeof initial === 'function' ? initial() : initial)
  }
  const set = value => {
    slots.set(key, typeof value === 'function' ? value(slots.get(key)) : value)
  }
  return [slots.get(key), set]
}

export function __begin(name) {
  const previous = current
  current = { name, index: 0 }
  return previous
}

export function __restore(previous) {
  current = previous
}

export function __resetHooks() {
  slots.clear()
  current = { name: 'root', index: 0 }
}
"""

HARNESS = r"""
import plugin from './plugin.mjs'
import { __begin, __resetHooks, __restore } from 'react'

const scenario = process.argv[2]

function clone(value) {
  return JSON.parse(JSON.stringify(value))
}

function renderNode(node) {
  if (node == null || node === false || node === true) return null
  if (Array.isArray(node)) return node.map(renderNode)
  if (typeof node !== 'object') return node
  if (typeof node.type === 'function') {
    const previous = __begin(node.type.name || 'anonymous')
    let output
    try {
      output = node.type(node.props || {})
    } finally {
      __restore(previous)
    }
    return renderNode(output)
  }
  const props = { ...(node.props || {}) }
  if ('children' in props) props.children = renderNode(props.children)
  return { type: node.type, props }
}

function collect(node, predicate, out = []) {
  if (node == null || node === false || typeof node !== 'object') return out
  if (Array.isArray(node)) {
    node.forEach(item => collect(item, predicate, out))
    return out
  }
  if (predicate(node)) out.push(node)
  if (node.props && 'children' in node.props) collect(node.props.children, predicate, out)
  return out
}

const baseValues = {
  timezone: 'UTC',
  destination: '',
  daily_normal_cap: 3,
  quiet_start_hour: 22,
  quiet_end_hour: 7,
  cooldown_minutes: 60,
  mail_enabled: false,
  calendar_enabled: false,
  tasks_enabled: false,
  public_feeds: false,
  obligations: false,
  public_interests: ['alpha'],
  discovery_mode: 'disabled',
  discovery_privacy_review: false,
  discovery_inferred_goals: false,
  collector_budget_seconds: 30,
  max_candidates_per_kind: 5,
  feed_max_sources_per_run: 3,
  preparation_enabled: false,
  preparation_source_consent: false,
  preparation_scopes: ['conversation'],
  public_sources: []
}

let server
let revisionCounter = 1
const calls = []
const writes = []

function publish() {
  globalThis.__settings = {
    data: { ok: true, revision: server.revision, values: clone(server.values) },
    isLoading: false,
    error: null,
    refetch: async () => {
      publish()
      return globalThis.__settings
    }
  }
}

function staleError() {
  return new Error(JSON.stringify({
    message: 'Settings changed elsewhere. Reload and review the current values.'
  }))
}

function ensureTypedLists(values) {
  for (const key of ['public_interests', 'preparation_scopes', 'public_sources']) {
    if (key in values && !Array.isArray(values[key])) {
      throw new Error(JSON.stringify({ message: key + ' must be a list.' }))
    }
  }
}

function expands(key, before, after) {
  if (key === 'public_interests' || key === 'preparation_scopes') {
    const prior = new Set((before || []).map(item => JSON.stringify(item)))
    return (after || []).some(item => !prior.has(JSON.stringify(item)))
  }
  return false
}

function makeServer() {
  server = {
    revision: 'rev-1',
    values: clone(baseValues)
  }
  calls.length = 0
  writes.length = 0
  revisionCounter = 1
  publish()
}

const ctx = {
  rest: async (path, options = {}) => {
    const method = options.method || 'GET'
    const body = options.body || {}
    calls.push({ path, method, body: clone(body) })
    if (path === '/settings/preview' && method === 'POST') {
      if (body.revision !== server.revision) throw staleError()
      ensureTypedLists(body.values || {})
      const expanding = []
      for (const key of ['public_interests', 'preparation_scopes']) {
        if (key in (body.values || {}) && expands(key, server.values[key], body.values[key])) {
          expanding.push(key)
        }
      }
      return {
        ok: true,
        changed: Object.keys(body.values || {}).length > 0,
        requires_confirmation: expanding.length > 0,
        expanding,
        expansion_details: expanding.map(key => ({ key, label: key, description: key }))
      }
    }
    if (path === '/settings' && method === 'POST') {
      if (body.revision !== server.revision) throw staleError()
      ensureTypedLists(body.values || {})
      const keys = Object.keys(body.values || {})
      if (keys.length) {
        server.values = { ...server.values, ...clone(body.values) }
        server.revision = 'rev-' + (++revisionCounter)
        writes.push(clone(body))
        publish()
      }
      return {
        ok: true,
        changed: keys.length > 0,
        changed_fields: keys,
        expanding: [],
        values: clone(server.values),
        revision: server.revision
      }
    }
    return {}
  }
}

let page = null
ctx.registerSettingsPage = value => { page = value }

globalThis.__status = {
  data: {
    ok: true,
    onboarding: { status: 'in_progress', label: 'Setup in progress' },
    config: { present: true, source: 'native', error: '' },
    permissions: { needs_reconfirmation: false, paused: false, reading_enabled: true },
    sources: {},
    model_routes: { strict_active: false, primary: {}, blockers: [] },
    jobs: { active_reminders: 0, owned_jobs: 0 }
  },
  isLoading: false,
  error: null,
  refetch: async () => {}
}

makeServer()
__resetHooks()
plugin.register(ctx)

let tree = null
function render() {
  tree = renderNode(page.render())
  return tree
}
render()

function find(predicate) {
  const found = collect(tree, predicate)
  return found.length ? found[0] : null
}
function input(label) {
  return find(node => node.type === 'input' && node.props['aria-label'] === label)
}
function button(label) {
  return find(node => node.type === 'button' && node.props.children === label)
}
function change(label, value) {
  const node = input(label)
  if (!node) throw new Error('input not found: ' + label)
  node.props.onChange({ target: { value } })
  render()
}
async function click(label) {
  const node = button(label)
  if (!node) throw new Error('button not found: ' + label)
  await node.props.onClick()
  render()
}
function texts() {
  const out = []
  collect(tree, node => node.type === 'p', out)
  return out.map(node => node.props && node.props.children).filter(value => typeof value === 'string')
}

async function staleRefetchScenario() {
  const before = input('Timezone').props.value
  change('Timezone', 'Europe/London')
  const edited = input('Timezone').props.value

  server.revision = 'rev-2'
  revisionCounter = 2
  server.values = { ...server.values, timezone: 'Asia/Tokyo', destination: 'newer@example.com' }
  publish()
  render()
  const whileStale = input('Timezone').props.value

  await click('Save settings')
  const firstPreviews = calls.filter(call => call.path === '/settings/preview')
  const writesAfterFirst = writes.length
  const noticeAfterReject = texts().join(' | ')
  const stillDraft = input('Timezone').props.value
  const destinationInDraft = input('Where messages are sent').props.value

  await click('Save settings')
  const writesAfterSecond = writes.length

  await click('Discard changes')
  const afterDiscardTimezone = input('Timezone').props.value
  const afterDiscardDestination = input('Where messages are sent').props.value

  change('Where messages are sent', 'mine@example.com')
  await click('Save settings')
  const final = writes[writes.length - 1] || null

  return {
    initial: before,
    edited,
    whileStale,
    previewRevisions: firstPreviews.map(call => call.body.revision),
    previewValues: firstPreviews.map(call => call.body.values),
    writesAfterReject: writesAfterFirst,
    writesAfterSecond,
    noticeAfterReject,
    stillDraft,
    destinationInDraft,
    afterDiscardTimezone,
    afterDiscardDestination,
    finalWrite: final,
    finalRevision: server.revision
  }
}

async function listScenario() {
  change('Public topics you care about', 'one,')
  const afterComma = input('Public topics you care about').props.value
  change('Public topics you care about', 'one, two')
  const afterSecond = input('Public topics you care about').props.value
  change('Public topics you care about', 'one, two, three')
  const afterPaste = input('Public topics you care about').props.value

  await click('Save settings')
  const preview = calls.filter(call => call.path === '/settings/preview').pop()
  const confirmPresent = Boolean(button('Confirm and save'))
  await click('Confirm and save')
  const interestWrite = writes[0] || null
  const savedText = input('Public topics you care about').props.value

  change('Public topics you care about', '')
  const emptyDraft = input('Public topics you care about').props.value
  await click('Save settings')
  const emptyWrite = writes[1] || null
  const emptySavedText = input('Public topics you care about').props.value

  change('What preparation may read', 'conversation,')
  const scopeAfterComma = input('What preparation may read').props.value
  change('What preparation may read', 'conversation, public')
  const scopeAfterSecond = input('What preparation may read').props.value
  await click('Save settings')
  const scopePreview = calls.filter(call => call.path === '/settings/preview').pop()
  const scopeConfirmPresent = Boolean(button('Confirm and save'))
  await click('Confirm and save')
  const scopeWrite = writes[2] || null
  const scopeSavedText = input('What preparation may read').props.value

  const writesBeforeDiscard = writes.length
  change('What preparation may read', 'conversation, public, tasks,')
  const discardDraftText = input('What preparation may read').props.value
  await click('Discard changes')
  const discardText = input('What preparation may read').props.value

  return {
    afterComma,
    afterSecond,
    afterPaste,
    previewValue: preview && preview.body.values.public_interests,
    previewRevision: preview && preview.body.revision,
    confirmPresent,
    interestWrite,
    savedText,
    emptyDraft,
    emptyWrite,
    emptySavedText,
    scopeAfterComma,
    scopeAfterSecond,
    scopePreviewValue: scopePreview && scopePreview.body.values.preparation_scopes,
    scopeConfirmPresent,
    scopeWrite,
    scopeSavedText,
    discardDraftText,
    discardText,
    writesBeforeDiscard,
    writesAfterDiscard: writes.length
  }
}

const result = scenario === 'stale'
  ? await staleRefetchScenario()
  : await listScenario()
console.log(JSON.stringify(result))
"""


class DraftStateInteractionTests(unittest.TestCase):
    def _run(self, scenario: str) -> dict:
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        scratch = tempfile.TemporaryDirectory(
            prefix="secretary-051-drafts-", dir=os.environ.get("TMPDIR") or None)
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        shutil.copy(ROOT / "desktop" / "plugin.js", root / "plugin.mjs")
        modules = root / "node_modules"
        sdk = modules / "@hermes" / "plugin-sdk"
        sdk.mkdir(parents=True)
        (sdk / "package.json").write_text(json.dumps(
            {"name": "@hermes/plugin-sdk", "type": "module", "exports": "./index.js"}),
            encoding="utf-8")
        (sdk / "index.js").write_text(SDK_STUB, encoding="utf-8")
        react = modules / "react"
        react.mkdir()
        (react / "package.json").write_text(json.dumps(
            {"name": "react", "type": "module",
             "exports": {".": "./index.js", "./jsx-runtime": "./jsx-runtime.js"}}),
            encoding="utf-8")
        (react / "index.js").write_text(REACT_STUB, encoding="utf-8")
        (react / "jsx-runtime.js").write_text(
            "export const jsx = (type, props) => ({ type, props: props || {} })\n"
            "export const jsxs = jsx\n", encoding="utf-8")
        harness = root / "harness.mjs"
        harness.write_text(HARNESS, encoding="utf-8")
        proc = subprocess.run([node, str(harness), scenario], cwd=str(root),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_refetch_while_draft_open_keeps_captured_revision_and_rejects_write(self):
        result = self._run("stale")
        self.assertEqual(result["initial"], "UTC")
        self.assertEqual(result["edited"], "Europe/London")
        self.assertEqual(result["whileStale"], "Europe/London")
        self.assertTrue(result["previewRevisions"])
        self.assertEqual(set(result["previewRevisions"]), {"rev-1"})
        self.assertEqual(result["writesAfterReject"], 0)
        self.assertEqual(result["writesAfterSecond"], 0)
        self.assertIn("Settings changed elsewhere", result["noticeAfterReject"])
        self.assertEqual(result["stillDraft"], "Europe/London")
        self.assertEqual(result["destinationInDraft"], "")
        self.assertEqual(result["afterDiscardTimezone"], "Asia/Tokyo")
        self.assertEqual(result["afterDiscardDestination"], "newer@example.com")
        self.assertEqual(result["finalWrite"]["revision"], "rev-2")
        self.assertEqual(list(result["finalWrite"]["values"]), ["destination"])
        self.assertEqual(result["finalWrite"]["values"]["destination"], "mine@example.com")
        self.assertEqual(result["finalRevision"], "rev-3")

    def test_list_fields_keep_raw_typing_and_save_typed_lists(self):
        result = self._run("list")
        self.assertEqual(result["afterComma"], "one,")
        self.assertEqual(result["afterSecond"], "one, two")
        self.assertEqual(result["afterPaste"], "one, two, three")
        self.assertEqual(result["previewValue"], ["one", "two", "three"])
        self.assertEqual(result["previewRevision"], "rev-1")
        self.assertTrue(result["confirmPresent"])
        self.assertEqual(result["interestWrite"]["revision"], "rev-1")
        self.assertEqual(result["interestWrite"]["values"]["public_interests"],
                         ["one", "two", "three"])
        self.assertEqual(result["savedText"], "one, two, three")

        self.assertEqual(result["emptyDraft"], "")
        self.assertEqual(result["emptyWrite"]["values"]["public_interests"], [])
        self.assertEqual(result["emptySavedText"], "")

        self.assertEqual(result["scopeAfterComma"], "conversation,")
        self.assertEqual(result["scopeAfterSecond"], "conversation, public")
        self.assertEqual(result["scopePreviewValue"], ["conversation", "public"])
        self.assertTrue(result["scopeConfirmPresent"])
        self.assertEqual(result["scopeWrite"]["values"]["preparation_scopes"],
                         ["conversation", "public"])
        self.assertEqual(result["scopeSavedText"], "conversation, public")

        self.assertEqual(result["discardDraftText"], "conversation, public, tasks,")
        self.assertEqual(result["discardText"], "conversation, public")
        self.assertEqual(result["writesAfterDiscard"], result["writesBeforeDiscard"])


if __name__ == "__main__":
    unittest.main()
