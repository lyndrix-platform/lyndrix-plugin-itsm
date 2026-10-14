const PLUGIN_ID = 'lyndrix.plugin.itsm'
const REGISTRY_ID = 'lyndrix.plugin.entity_registry'
const TOKEN_KEY = 'lyndrix_token'
const REQUEST_TIMEOUT_MS = 20000

// ─── Catalog types (mirror app/logic/catalog.py) ─────────────────────────────

export interface Option { id: string | number; label: string; color?: string }
export interface Category { id: string; label: string; children: Category[] }
export interface FieldDef {
  id: string
  label: string
  kind: 'text' | 'select' | 'int' | 'bool' | 'datetime'
  options: Option[]
  multiline: boolean
  description: string
}
export interface StateDef {
  id: string
  label: string
  kind: 'open' | 'resolved' | 'closed'
  initial: boolean
  final: boolean
  requires: string[]
}
export interface RecordTypeDef {
  id: string
  label: string
  prefix: string
  icon: string
  mode: string
  description: string
  subtypes: Option[]
  categories: Category[]
  priority: {
    impact: Option[]
    urgency: Option[]
    defaults: Record<string, number>
    matrix: Record<string, string>
    levels: Option[]
  } | null
  states: StateDef[]
  transitions: Record<string, string[]>
  fields: FieldDef[]
  assignment_groups: { id: string; label: string; core_group: string | null }[]
}
export interface Catalog {
  record_types: RecordTypeDef[]
  record_link_relations: Record<string, string>
  ci_link_roles: string[]
}

// ─── Record types (mirror app/model/models.py to_dict) ───────────────────────

export interface RecordSummary {
  uuid: string
  number: string
  record_type: string
  subtype: string | null
  category: string | null
  title: string
  description: string | null
  state: string
  impact: number | null
  urgency: number | null
  priority: string | null
  assignment_group: string | null
  assignee: string | null
  requester: string | null
  created_by: string | null
  custom_fields: Record<string, unknown>
  tags: string[]
  opened_at: string | null
  resolved_at: string | null
  closed_at: string | null
  due_at: string | null
  version: number
  source: string | null
  external_id: string | null
  external_url: string | null
  sync_state: string | null
  sync_error: string | null
  ci_count?: number
}
export interface JournalEntry {
  id: number
  kind: string
  author: string | null
  body: string | null
  data: Record<string, unknown>
  created_at: string | null
}
export interface CiLink {
  id: number
  entity_uuid: string
  role: string
  display_name: string | null
  entity_type: string | null
}
export interface RecordLinkView {
  id: number
  relation: string
  direction: 'in' | 'out'
  relation_from_here: string
  other_uuid: string
  other: { number: string; title: string; record_type: string; state: string; priority: string | null } | null
}
export interface AttachmentMeta {
  uuid: string
  filename: string
  content_type: string
  size: number
  sha256: string
  uploaded_by: string | null
  created_at: string | null
}
export interface RecordDetail extends RecordSummary {
  allowed_transitions: string[]
  journal: JournalEntry[]
  ci_links: CiLink[]
  links: RecordLinkView[]
  attachments: AttachmentMeta[]
}
export interface Stats {
  open: number
  total: number
  open_by_type: Record<string, number>
  open_by_priority: Record<string, number>
}
export interface RegistryEntity {
  uuid: string
  entity_type: string
  display_name: string
  identifiers: { scheme: string; value: string }[]
}

// ─── Fetch wrapper ────────────────────────────────────────────────────────────

function spaRedirect(path: string) {
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = localStorage.getItem(TOKEN_KEY)
  return { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(extra ?? {}) }
}

async function rawFetch(path: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  let res: Response
  try {
    res = await fetch(path, { ...init, signal: controller.signal })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw new Error('Request timed out')
    throw err
  } finally {
    window.clearTimeout(timer)
  }
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    spaRedirect('/login')
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      msg = body.detail ?? msg
    } catch { /* ignore */ }
    const e = new Error(msg) as Error & { status?: number }
    e.status = res.status
    throw e
  }
  return res
}

async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await rawFetch(path, {
    ...init,
    headers: authHeaders({ 'Content-Type': 'application/json', ...((init.headers as Record<string, string>) ?? {}) }),
  })
  return res.json() as Promise<T>
}

// Always encode user-controlled path params so '/' or '..' cannot alter the route.
const enc = (v: string | number) => encodeURIComponent(String(v))
const base = (sub: string) => `/api/plugins/${PLUGIN_ID}/${sub}`

export const itsmApi = {
  list: (p: {
    type?: string; state?: string; priority?: string; assignee?: string; group?: string
    tag?: string; ci?: string; q?: string; include_closed?: boolean; limit?: number
  }) => {
    const qs = new URLSearchParams()
    Object.entries(p).forEach(([k, v]) => {
      if (v !== undefined && v !== '' && v !== false) qs.set(k, String(v))
    })
    return apiJson<{ total: number; records: RecordSummary[] }>(base(`records?${qs}`))
  },
  get: (ref: string) => apiJson<{ record: RecordDetail }>(base(`records/${enc(ref)}`)),
  create: (body: Record<string, unknown>) =>
    apiJson<{ record: RecordDetail }>(base('records'), { method: 'POST', body: JSON.stringify(body) }),
  patch: (ref: string, body: Record<string, unknown>) =>
    apiJson<{ record: RecordDetail }>(base(`records/${enc(ref)}`), { method: 'PATCH', body: JSON.stringify(body) }),
  transition: (ref: string, body: { to_state: string; fields: Record<string, unknown>; note?: string; version?: number }) =>
    apiJson<{ record: RecordDetail }>(base(`records/${enc(ref)}/transition`), { method: 'POST', body: JSON.stringify(body) }),
  addJournal: (ref: string, kind: string, text: string) =>
    apiJson<{ entry: JournalEntry }>(base(`records/${enc(ref)}/journal`), { method: 'POST', body: JSON.stringify({ kind, body: text }) }),
  linkCi: (ref: string, body: { entity_uuid: string; role: string; display_name?: string | null; entity_type?: string | null }) =>
    apiJson<{ record: RecordDetail }>(base(`records/${enc(ref)}/ci-links`), { method: 'POST', body: JSON.stringify(body) }),
  unlinkCi: (ref: string, role: string, entity_uuid: string) =>
    apiJson<{ record: RecordDetail }>(base(`records/${enc(ref)}/ci-links/${enc(role)}/${enc(entity_uuid)}`), { method: 'DELETE' }),
  linkRecord: (ref: string, to: string, relation: string) =>
    apiJson<{ record: RecordDetail }>(base(`records/${enc(ref)}/links`), { method: 'POST', body: JSON.stringify({ to, relation }) }),
  unlinkRecord: (ref: string, relation: string, to: string) =>
    apiJson<{ record: RecordDetail }>(base(`records/${enc(ref)}/links/${enc(relation)}/${enc(to)}`), { method: 'DELETE' }),
  upload: async (ref: string, file: File) => {
    const form = new FormData()
    form.append('file', file, file.name)
    // No Content-Type: the browser sets the multipart boundary itself.
    const res = await rawFetch(base(`records/${enc(ref)}/attachments`), { method: 'POST', body: form, headers: authHeaders() })
    return res.json() as Promise<{ attachment: AttachmentMeta }>
  },
  download: async (ref: string, att: AttachmentMeta) => {
    const res = await rawFetch(base(`records/${enc(ref)}/attachments/${enc(att.uuid)}`), { headers: authHeaders() })
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = att.filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 5000)
  },
  deleteAttachment: (ref: string, attUuid: string) =>
    apiJson<{ ok: boolean }>(base(`records/${enc(ref)}/attachments/${enc(attUuid)}`), { method: 'DELETE' }),
  catalog: () => apiJson<Catalog>(base('catalog')),
  reloadCatalog: () => apiJson<Catalog>(base('catalog/reload'), { method: 'POST' }),
  stats: () => apiJson<Stats>(base('stats')),
}

// Best-effort access to the Entity Registry for the CI picker. Any failure
// (plugin absent, no permission) just disables the search — manual UUID
// entry keeps working.
export const registryApi = {
  search: async (q: string): Promise<RegistryEntity[]> => {
    try {
      const res = await apiJson<{ entities: RegistryEntity[] }>(
        `/api/plugins/${REGISTRY_ID}/entities?q=${encodeURIComponent(q)}&limit=15`,
      )
      return res.entities
    } catch {
      return []
    }
  },
  deepLink: (uuid: string) => `/apps/lyndrix-plugin-entity_registry/registry?uuid=${encodeURIComponent(uuid)}`,
}
