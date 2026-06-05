#!/usr/bin/env node
'use strict'

/**
 * Minimal Huly read bridge.
 *
 * Huly has no Python SDK and no plain REST resource API — data is read through
 * the official TS SDK (@hcengineering/api-client) against the platform's typed
 * document model. This script connects (WebSocket via connect — it loads the
 * platform model, which the REST client does not), runs one of a few read
 * queries, and prints the result as JSON on stdout so the Python daily-agent
 * can consume it.
 *
 * Usage:
 *   node index.js projects
 *   node index.js issues --project ENG [--limit 50]
 *   node index.js issue ENG-14826
 *
 * Auth/config via env (HULY_* preferred, DAILY_AGENT_HULY_* fallback):
 *   HULY_URL (default https://huly.app), HULY_WORKSPACE,
 *   HULY_TOKEN  OR  HULY_EMAIL + HULY_PASSWORD
 */

// Keep stdout pristine for JSON: the platform client emits model-load warnings
// ("no document found, failed to apply model transaction, skipping ...") via
// console.*; redirect all of those to stderr.
for (const m of ['log', 'info', 'warn', 'debug', 'error']) {
  console[m] = (...args) => process.stderr.write(args.map(String).join(' ') + '\n')
}

const apiClient = require('@hcengineering/api-client')
const coreMod = require('@hcengineering/core')
const trackerMod = require('@hcengineering/tracker')
const contactMod = require('@hcengineering/contact')
const taskMod = require('@hcengineering/task')

const core = coreMod.default
const tracker = trackerMod.default
const contact = contactMod.default
const task = taskMod.default
const SortingOrder = coreMod.SortingOrder

const PRIORITY_LABELS = { 0: 'none', 1: 'urgent', 2: 'high', 3: 'medium', 4: 'low' }
const PRIORITY_VALUES = { none: 0, urgent: 1, high: 2, medium: 3, low: 4 }

function env (name) {
  return process.env['HULY_' + name] || process.env['DAILY_AGENT_HULY_' + name] || ''
}

function formatName (name) {
  if (!name) return null
  if (name.includes(',')) {
    const [last, first] = name.split(',')
    return `${first} ${last}`.trim()
  }
  return name
}

function categoryLabel (cat) {
  if (!cat) return 'unknown'
  if (cat === task.statusCategory.Won) return 'done'
  if (cat === task.statusCategory.Lost) return 'cancelled'
  if (cat === task.statusCategory.Active) return 'active'
  if (cat === task.statusCategory.ToDo) return 'todo'
  if (cat === task.statusCategory.UnStarted) return 'backlog'
  return 'unknown'
}

async function getClient () {
  const url = env('URL') || 'https://huly.app'
  const workspace = env('WORKSPACE')
  const token = env('TOKEN')
  const email = env('EMAIL')
  const password = env('PASSWORD')
  if (!workspace) throw new Error('Missing HULY_WORKSPACE')
  if (!token && (!email || !password)) {
    throw new Error('Missing credentials: set HULY_TOKEN or HULY_EMAIL + HULY_PASSWORD')
  }
  const auth = token ? { token, workspace } : { email, password, workspace }
  // WebSocket client (loads the platform model — required to resolve Status,
  // Person, etc.). The REST client does not load the model and fails on those.
  return apiClient.connect(url, auth)
}

async function buildStatusMap (client) {
  const statuses = await client.findAll(core.class.Status, {})
  const map = new Map()
  for (const s of statuses) map.set(s._id, { name: s.name, category: s.category })
  return map
}

async function buildAssigneeMap (client, issues) {
  const ids = [...new Set(issues.filter((i) => i.assignee).map((i) => i.assignee))]
  const map = new Map()
  if (ids.length === 0) return map
  const persons = await client.findAll(contact.class.Person, { _id: { $in: ids } })
  for (const p of persons) map.set(p._id, formatName(p.name))
  return map
}

function issueRow (i, statusMap, assigneeMap) {
  const st = statusMap.get(i.status)
  return {
    identifier: i.identifier,
    title: i.title,
    status: st ? st.name : 'Unknown',
    statusCategory: categoryLabel(st ? st.category : undefined),
    assignee: i.assignee ? assigneeMap.get(i.assignee) || null : null,
    priority: PRIORITY_LABELS[i.priority] || 'none',
    estimation: i.estimation || 0,
    modifiedOn: i.modifiedOn ? new Date(i.modifiedOn).toISOString() : null,
    dueDate: i.dueDate ? new Date(i.dueDate).toISOString() : null
  }
}

async function resolveProjectId (client, identifier) {
  const projects = await client.findAll(tracker.class.Project, {})
  const match = projects.find((p) => p.identifier.toLowerCase() === identifier.toLowerCase())
  if (!match) throw new Error('Project not found: ' + identifier)
  return match._id
}

async function listProjects (client) {
  const projects = await client.findAll(tracker.class.Project, {})
  return projects.map((p) => ({
    identifier: p.identifier,
    name: p.name,
    description: p.description || ''
  }))
}

async function listIssues (client, opts) {
  const { project, status, assignee, priority } = opts
  const limit = opts.limit || 50
  const statusMap = await buildStatusMap(client)
  const query = {}
  if (project) query.space = await resolveProjectId(client, project)
  if (status) {
    const match = [...statusMap.entries()].find(
      ([, v]) => v.name.toLowerCase() === status.toLowerCase()
    )
    if (!match) {
      const names = [...new Set([...statusMap.values()].map((v) => v.name))].join(', ')
      throw new Error(`Status not found: "${status}". Known: ${names}`)
    }
    query.status = match[0]
  }
  if (priority) {
    const pv = PRIORITY_VALUES[priority.toLowerCase()]
    if (pv === undefined) throw new Error(`Invalid priority: "${priority}" (none|urgent|high|medium|low)`)
    query.priority = pv
  }
  // When filtering by assignee (a post-query name match), fetch a wider set
  // first so the limit applies to the filtered results.
  const fetchLimit = assignee ? Math.max(limit, 500) : limit
  let issues = await client.findAll(tracker.class.Issue, query, {
    limit: fetchLimit,
    sort: { modifiedOn: SortingOrder.Descending }
  })
  const assigneeMap = await buildAssigneeMap(client, issues)
  if (assignee) {
    const q = assignee.toLowerCase()
    issues = issues
      .filter((i) => {
        const name = i.assignee ? assigneeMap.get(i.assignee) : null
        return name && name.toLowerCase().includes(q)
      })
      .slice(0, limit)
  }
  return issues.map((i) => issueRow(i, statusMap, assigneeMap))
}

async function getIssue (client, identifier) {
  const issue = await client.findOne(tracker.class.Issue, { identifier: identifier.toUpperCase() })
  if (!issue) return null
  const statusMap = await buildStatusMap(client)
  const assigneeMap = await buildAssigneeMap(client, [issue])
  const projects = await client.findAll(tracker.class.Project, {})
  const project = projects.find((p) => p._id === issue.space)
  let description = null
  if (issue.description && typeof client.fetchMarkup === 'function') {
    try {
      description = await client.fetchMarkup(
        tracker.class.Issue, issue._id, 'description', issue.description, 'markdown'
      )
    } catch (_) { description = null }
  }
  return {
    ...issueRow(issue, statusMap, assigneeMap),
    project: project ? project.identifier : 'Unknown',
    number: issue.number,
    description
  }
}

function flag (args, name) {
  const idx = args.indexOf(name)
  return idx >= 0 ? args[idx + 1] : undefined
}

async function main () {
  const [cmd, ...rest] = process.argv.slice(2)
  const client = await getClient()
  try {
    let out
    if (cmd === 'projects') {
      out = await listProjects(client)
    } else if (cmd === 'issues') {
      const limit = flag(rest, '--limit')
      out = await listIssues(client, {
        project: flag(rest, '--project'),
        status: flag(rest, '--status'),
        assignee: flag(rest, '--assignee'),
        priority: flag(rest, '--priority'),
        limit: limit ? parseInt(limit, 10) : 50
      })
    } else if (cmd === 'issue') {
      if (!rest[0]) throw new Error('Usage: issue <IDENTIFIER>')
      out = await getIssue(client, rest[0])
    } else {
      throw new Error('Unknown command: ' + cmd + ' (expected: projects | issues | issue)')
    }
    process.stdout.write(JSON.stringify(out))
  } finally {
    if (typeof client.close === 'function') await client.close()
  }
}

main().catch((e) => {
  process.stderr.write(String((e && e.message) || e))
  process.exit(1)
})
