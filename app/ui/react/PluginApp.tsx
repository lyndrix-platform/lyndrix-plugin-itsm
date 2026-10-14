import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
// Provided by the host shell (window.__lyndrix_react_i18next); strings come
// from locales/itsm.<locale>.json (namespace "itsm").
import { useTranslation } from 'react-i18next'
import { isUuid, itsmApi, registryApi } from './lib/api'
import type {
  AttachmentMeta, Catalog, FieldDef, RecordDetail, RecordSummary, RecordTypeDef, RegistryEntity, Stats,
} from './lib/api'

// ─── Shared pieces ────────────────────────────────────────────────────────────

function Button({ label, onClick, disabled, variant = 'default', icon, title, type = 'button' }: {
  label: string
  onClick?: () => void
  disabled?: boolean
  variant?: 'default' | 'danger' | 'primary' | 'ghost'
  icon?: string
  title?: string
  type?: 'button' | 'submit'
}) {
  const cls =
    variant === 'primary' ? 'lx-btn lx-btn--primary lx-btn--sm'
    : variant === 'danger' ? 'lx-btn lx-btn--danger lx-btn--sm'
    : variant === 'ghost' ? 'lx-btn lx-btn--ghost lx-btn--sm'
    : 'lx-btn lx-btn--secondary lx-btn--sm'
  return (
    <button type={type} onClick={onClick} disabled={disabled} className={cls} title={title}>
      {icon && <span className="material-icons" style={{ fontSize: 16 }}>{icon}</span>}
      {label}
    </button>
  )
}

function ErrorBox({ msg }: { msg: string }) {
  return (
    <div className="lx-card lx-card-accent lx-card-accent--danger" style={{ padding: '0.75rem 1rem', marginBottom: '1rem' }}>
      <span style={{ color: 'var(--lx-state-down)', fontSize: '0.85rem' }}>{msg}</span>
    </div>
  )
}

function fmt(ts: string | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts.endsWith('Z') || ts.includes('+') ? ts : ts + 'Z')
  return isNaN(d.getTime()) ? ts : d.toLocaleString()
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

const typeOf = (catalog: Catalog | null, id: string) => catalog?.record_types.find((t) => t.id === id) ?? null

function TypeBadge({ type, catalog }: { type: string; catalog: Catalog | null }) {
  const rt = typeOf(catalog, type)
  return (
    <span className="lx-badge lx-badge--accent">
      <span className="material-icons" style={{ fontSize: 13 }}>{rt?.icon ?? 'assignment'}</span>
      {rt?.label ?? type}
    </span>
  )
}

function StateBadge({ record, catalog }: { record: RecordSummary; catalog: Catalog | null }) {
  const rt = typeOf(catalog, record.record_type)
  const st = rt?.states.find((s) => s.id === record.state)
  const mod = st?.kind === 'closed' ? 'lx-badge--muted' : st?.kind === 'resolved' ? 'lx-badge--success' : 'lx-badge--up'
  return <span className={`lx-badge ${mod}`}><span className="lx-dot" />{st?.label ?? record.state}</span>
}

function PriorityBadge({ record, catalog }: { record: RecordSummary; catalog: Catalog | null }) {
  if (!record.priority) return <span className="lx-badge lx-badge--muted">—</span>
  const rt = typeOf(catalog, record.record_type)
  const lvl = rt?.priority?.levels.find((l) => String(l.id) === record.priority)
  const color = lvl?.color ?? 'unknown'
  const mod = ['down', 'paused', 'unknown', 'success', 'up'].includes(color) ? `lx-badge--${color}` : 'lx-badge--muted'
  return <span className={`lx-badge ${mod}`} title={lvl?.label}>{record.priority}</span>
}

function flattenCategories(rt: RecordTypeDef | null): { id: string; label: string }[] {
  const out: { id: string; label: string }[] = []
  const walk = (cats: RecordTypeDef['categories'], depth: number) => {
    for (const c of cats) {
      out.push({ id: c.id, label: `${'  '.repeat(depth)}${c.label}` })
      walk(c.children, depth + 1)
    }
  }
  if (rt) walk(rt.categories, 0)
  return out
}

/** Renders one catalog custom field by kind. */
function FieldInput({ field, value, onChange }: { field: FieldDef; value: unknown; onChange: (v: unknown) => void }) {
  const common = { className: 'lx-input', style: { width: '100%' } as React.CSSProperties }
  if (field.kind === 'select') {
    return (
      <select className="lx-select" style={{ width: '100%' }} value={value == null ? '' : String(value)} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">—</option>
        {field.options.map((o) => <option key={String(o.id)} value={String(o.id)}>{o.label}</option>)}
      </select>
    )
  }
  if (field.kind === 'bool') {
    return (
      <label style={{ display: 'inline-flex', gap: '0.4rem', alignItems: 'center', fontSize: '0.85rem' }}>
        <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} /> {field.label}
      </label>
    )
  }
  if (field.kind === 'int') {
    return <input {...common} type="number" value={value == null ? '' : String(value)} onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))} />
  }
  if (field.kind === 'datetime') {
    return <input {...common} type="datetime-local" value={value ? String(value).slice(0, 16) : ''} onChange={(e) => onChange(e.target.value ? new Date(e.target.value).toISOString() : null)} />
  }
  if (field.multiline) {
    return <textarea className="lx-textarea" style={{ width: '100%', minHeight: 80 }} value={value == null ? '' : String(value)} onChange={(e) => onChange(e.target.value)} />
  }
  return <input {...common} type="text" value={value == null ? '' : String(value)} onChange={(e) => onChange(e.target.value)} />
}

function Labeled({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
      <span className="lx-label">{label}</span>
      {children}
      {hint && <span style={{ fontSize: '0.7rem', color: 'var(--lx-text-muted)' }}>{hint}</span>}
    </div>
  )
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="lx-modal-overlay" onClick={onClose} style={{ position: 'fixed', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 60 }}>
      <div className="lx-modal lx-card" onClick={(e) => e.stopPropagation()} style={{ width: 'min(640px, 94vw)', maxHeight: '90vh', overflow: 'auto', padding: '1.25rem' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
          <h3 style={{ margin: 0, fontSize: '1rem', fontWeight: 700, color: 'var(--lx-text)' }}>{title}</h3>
          <button className="lx-icon-btn" onClick={onClose}><span className="material-icons" style={{ fontSize: 18 }}>close</span></button>
        </div>
        {children}
      </div>
    </div>
  )
}

type CiPick = { entity_uuid: string; display_name: string | null; entity_type: string | null; role: string }

/** Entity Registry picker. Type a name/identifier (docker-hydra) or paste a
 *  UUID: the registry resolves it and the match is shown *before* it is linked,
 *  so a typo never becomes a dangling link. A raw UUID can only be linked blind
 *  when the registry is unreachable. */
function CiPicker({ onPick, roles, linked = [], autoFocus }: {
  onPick: (e: CiPick) => void
  roles: string[]
  linked?: { entity_uuid: string; role: string }[]
  autoFocus?: boolean
}) {
  const { t } = useTranslation('itsm')
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<RegistryEntity[]>([])
  const [status, setStatus] = useState<'idle' | 'searching' | 'none' | 'unknown_uuid' | 'unavailable'>('idle')
  const [role, setRole] = useState(roles[0] ?? 'affected')
  const term = q.trim()
  const uuidTyped = isUuid(term)

  useEffect(() => {
    if (term.length < 2) { setHits([]); setStatus('idle'); return }
    let cancelled = false
    setStatus('searching')
    const h = window.setTimeout(async () => {
      if (uuidTyped) {
        const res = await registryApi.get(term)
        if (cancelled) return
        setHits(res.entity ? [res.entity] : [])
        setStatus(res.entity ? 'idle' : res.available ? 'unknown_uuid' : 'unavailable')
      } else {
        const found = await registryApi.search(term)
        if (cancelled) return
        setHits(found)
        setStatus(found.length ? 'idle' : 'none')
      }
    }, 250)
    return () => { cancelled = true; window.clearTimeout(h) }
  }, [term, uuidTyped])

  const isLinked = (uuid: string) => linked.some((l) => l.entity_uuid === uuid && l.role === role)
  const pick = (h: RegistryEntity) => {
    if (isLinked(h.uuid)) return
    onPick({ entity_uuid: h.uuid, display_name: h.display_name, entity_type: h.entity_type, role })
    setQ(''); setHits([]); setStatus('idle')
  }
  const hint = (key: string, fallback: string, danger = false) => (
    <div style={{ fontSize: '0.72rem', color: danger ? 'var(--lx-state-down)' : 'var(--lx-text-muted)' }}>{t(key, { defaultValue: fallback })}</div>
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
      <div style={{ display: 'flex', gap: '0.4rem' }}>
        <select className="lx-select" style={{ flex: '0 0 150px', width: 'auto' }} value={role} onChange={(e) => setRole(e.target.value)}>
          {roles.map((r) => <option key={r} value={r}>{t(`ci.role.${r}`, { defaultValue: r })}</option>)}
        </select>
        <input className="lx-input" style={{ flex: 1 }} autoFocus={autoFocus}
          placeholder={t('ci.searchPlaceholder', { defaultValue: 'Search the Entity Registry or paste a UUID…' })}
          title={t('ci.searchHint', { defaultValue: 'Type a name (e.g. docker-hydra), an identifier or a UUID — pick a result to link it.' })}
          value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && hits[0]) { e.preventDefault(); pick(hits[0]) } }} />
        {uuidTyped && status === 'unavailable' && (
          <Button label={t('ci.addAnyway', { defaultValue: 'Link UUID anyway' })} icon="link"
            onClick={() => { onPick({ entity_uuid: term, display_name: null, entity_type: null, role }); setQ(''); setStatus('idle') }} />
        )}
      </div>
      {status === 'searching' && hint('ci.resolving', 'Resolving configuration item…')}
      {status === 'none' && hint('ci.noMatch', 'No matching configuration item in the Entity Registry.')}
      {status === 'unknown_uuid' && hint('ci.unknownUuid', 'No configuration item with this UUID in the Entity Registry.', true)}
      {status === 'unavailable' && hint('ci.registryUnavailable', 'Entity Registry not reachable — you can still link a raw UUID.')}
      {hits.length > 0 && (
        <div className="lx-card lx-elevated" style={{ padding: '0.25rem 0', maxHeight: 200, overflow: 'auto' }}>
          {hits.map((h) => {
            const dup = isLinked(h.uuid)
            return (
              <div key={h.uuid} className={dup ? '' : 'lx-row-hover'}
                style={{ padding: '0.35rem 0.75rem', cursor: dup ? 'default' : 'pointer', opacity: dup ? 0.55 : 1, display: 'flex', gap: '0.5rem', alignItems: 'center', fontSize: '0.8rem' }}
                onClick={() => pick(h)}>
                <span className="lx-badge lx-badge--muted">{h.entity_type}</span>
                <span style={{ fontWeight: 600 }}>{h.display_name}</span>
                <span className="lx-mono" style={{ fontSize: '0.68rem', color: 'var(--lx-text-muted)', flex: 1 }}>{h.identifiers.slice(0, 2).map((i) => `${i.scheme}=${i.value}`).join(' · ')}</span>
                {dup && <span className="lx-badge lx-badge--muted">{t('ci.alreadyLinked', { defaultValue: 'already linked' })}</span>}
                <span className="lx-mono" style={{ fontSize: '0.62rem', color: 'var(--lx-text-muted)' }}>{h.uuid.slice(0, 8)}</span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function useCatalog() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    itsmApi.catalog().then((c) => { setCatalog(c); setError(null) }).catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])
  useEffect(() => { load() }, [load])
  return { catalog, error, reload: load, setCatalog }
}

// ─── Views ────────────────────────────────────────────────────────────────────

type View =
  | { kind: 'list'; ci?: string }
  | { kind: 'detail'; ref: string }
  | { kind: 'create'; type?: string; ci?: string }
  | { kind: 'catalog' }

function StatsStrip({ stats, catalog }: { stats: Stats | null; catalog: Catalog | null }) {
  const { t } = useTranslation('itsm')
  if (!stats) return null
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center', margin: '0.75rem 0 1rem' }}>
      <span className="lx-badge lx-badge--accent">{t('stats.open', { defaultValue: '{{count}} open', count: stats.open })}</span>
      {Object.entries(stats.open_by_type).map(([type, n]) => (
        <span key={type} className="lx-badge lx-badge--muted">{typeOf(catalog, type)?.label ?? type}: {n}</span>
      ))}
      {['P1', 'P2'].map((p) => stats.open_by_priority[p] ? (
        <span key={p} className="lx-badge lx-badge--down">{p}: {stats.open_by_priority[p]}</span>
      ) : null)}
      <span className="lx-badge lx-badge--muted">{t('stats.total', { defaultValue: '{{count}} total', count: stats.total })}</span>
    </div>
  )
}

function RecordList({ catalog, onOpen, initialCi }: { catalog: Catalog | null; onOpen: (ref: string) => void; initialCi?: string }) {
  const { t } = useTranslation('itsm')
  const [records, setRecords] = useState<RecordSummary[]>([])
  const [total, setTotal] = useState(0)
  const [q, setQ] = useState('')
  const [type, setType] = useState('')
  const [state, setState] = useState('')
  const [priority, setPriority] = useState('')
  const [includeClosed, setIncludeClosed] = useState(false)
  // Optional CI filter (deep link /itsm?ci=<uuid>, or "records for this CI").
  const [ci, setCi] = useState(initialCi ?? '')
  const [ciName, setCiName] = useState<string | null>(null)
  useEffect(() => {
    if (!ci) { setCiName(null); return }
    void registryApi.get(ci).then((r) => setCiName(r.entity ? `${r.entity.display_name} · ${r.entity.entity_type}` : null))
  }, [ci])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const rt = typeOf(catalog, type)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await itsmApi.list({ q: q.trim() || undefined, type: type || undefined, state: state || undefined, priority: priority || undefined, ci: ci || undefined, include_closed: includeClosed, limit: 500 })
      setRecords(res.records)
      setTotal(res.total)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [q, type, state, priority, ci, includeClosed])

  useEffect(() => {
    const h = window.setTimeout(() => void load(), q ? 250 : 0)
    return () => window.clearTimeout(h)
  }, [load, q])

  return (
    <>
      <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', marginBottom: '0.75rem', alignItems: 'center' }}>
        <input className="lx-input" style={{ flex: '1 1 200px' }} placeholder={t('list.searchPlaceholder', { defaultValue: 'Search number or title…' })} value={q} onChange={(e) => setQ(e.target.value)} />
        <select className="lx-select" style={{ flex: '0 0 160px', width: 'auto' }} value={type} onChange={(e) => { setType(e.target.value); setState('') }}>
          <option value="">{t('list.allTypes', { defaultValue: 'All types' })}</option>
          {catalog?.record_types.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
        </select>
        <select className="lx-select" style={{ flex: '0 0 160px', width: 'auto' }} value={state} onChange={(e) => setState(e.target.value)} disabled={!rt}>
          <option value="">{t('list.allStates', { defaultValue: 'All states' })}</option>
          {rt?.states.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
        </select>
        <select className="lx-select" style={{ flex: '0 0 150px', width: 'auto' }} value={priority} onChange={(e) => setPriority(e.target.value)}>
          <option value="">{t('list.allPriorities', { defaultValue: 'All priorities' })}</option>
          {['P1', 'P2', 'P3', 'P4'].map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <label style={{ display: 'inline-flex', gap: '0.35rem', alignItems: 'center', fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>
          <input type="checkbox" checked={includeClosed} onChange={(e) => setIncludeClosed(e.target.checked)} />
          {t('list.includeClosed', { defaultValue: 'include closed' })}
        </label>
        {ci && (
          <span className="lx-badge lx-badge--accent" style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3rem' }}>
            <span className="material-icons" style={{ fontSize: 13 }}>fingerprint</span>
            {t('list.ciFilter', { defaultValue: 'Configuration item' })}: {ciName ?? ci.slice(0, 8)}
            <button className="lx-icon-btn" title={t('list.ciFilterClear', { defaultValue: 'Remove CI filter' })} onClick={() => setCi('')}><span className="material-icons" style={{ fontSize: 13 }}>close</span></button>
          </span>
        )}
        <Button label={t('common.refresh', { defaultValue: 'Refresh' })} icon="refresh" onClick={() => void load()} />
      </div>
      {error && <ErrorBox msg={error} />}
      {loading ? (
        <div className="lx-empty"><span className="lx-spinner" /></div>
      ) : records.length === 0 ? (
        <div className="lx-empty">{t('list.empty', { defaultValue: 'No records match.' })}</div>
      ) : (
        <div className="lx-card" style={{ padding: 0, overflow: 'hidden' }}>
          <table className="lx-table">
            <thead>
              <tr>
                <th>{t('field.number', { defaultValue: 'Number' })}</th>
                <th>{t('field.title', { defaultValue: 'Title' })}</th>
                <th>{t('field.type', { defaultValue: 'Type' })}</th>
                <th>{t('field.state', { defaultValue: 'State' })}</th>
                <th>{t('field.priority', { defaultValue: 'Prio' })}</th>
                <th>{t('field.assignee', { defaultValue: 'Assignee' })}</th>
                <th>{t('field.opened', { defaultValue: 'Opened' })}</th>
              </tr>
            </thead>
            <tbody>
              {records.map((r) => (
                <tr key={r.uuid} className="lx-row-hover" style={{ cursor: 'pointer' }} onClick={() => onOpen(r.uuid)}>
                  <td className="lx-mono" style={{ fontSize: '0.78rem', whiteSpace: 'nowrap' }}>{r.number}</td>
                  <td>
                    <div style={{ fontWeight: 600, color: 'var(--lx-text)' }}>{r.title}</div>
                    {r.tags.length > 0 && <div style={{ fontSize: '0.68rem', color: 'var(--lx-text-muted)' }}>{r.tags.join(', ')}</div>}
                  </td>
                  <td><TypeBadge type={r.record_type} catalog={catalog} /></td>
                  <td><StateBadge record={r} catalog={catalog} /></td>
                  <td><PriorityBadge record={r} catalog={catalog} /></td>
                  <td style={{ fontSize: '0.78rem' }}>{r.assignee ?? <span style={{ color: 'var(--lx-text-muted)' }}>—</span>}</td>
                  <td style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)', whiteSpace: 'nowrap' }}>{fmt(r.opened_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ padding: '0.5rem 0.75rem', fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>
            {t('list.shown', { defaultValue: '{{shown}} of {{total}}', shown: records.length, total })}
          </div>
        </div>
      )}
    </>
  )
}

function CreateForm({ catalog, initialType, initialCi, onCreated, onCancel }: {
  catalog: Catalog | null
  initialType?: string
  initialCi?: string
  onCreated: (ref: string) => void
  onCancel: () => void
}) {
  const { t } = useTranslation('itsm')
  const [type, setType] = useState(initialType ?? catalog?.record_types[0]?.id ?? 'incident')
  const rt = typeOf(catalog, type)
  const [form, setForm] = useState<Record<string, unknown>>({})
  const [custom, setCustom] = useState<Record<string, unknown>>({})
  const [ciLinks, setCiLinks] = useState<CiPick[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const set = (k: string, v: unknown) => setForm((f) => ({ ...f, [k]: v }))

  // Deep link /itsm?new=incident&ci=<uuid> (Server Manager / Entity Registry
  // "report incident"): resolve the CI so the form shows its name, not a UUID.
  useEffect(() => {
    if (!initialCi) return
    void registryApi.get(initialCi).then((r) => {
      if (r.entity) {
        setCiLinks([{ entity_uuid: r.entity.uuid, display_name: r.entity.display_name, entity_type: r.entity.entity_type, role: 'affected' }])
      } else if (!r.available) {
        setCiLinks([{ entity_uuid: initialCi, display_name: null, entity_type: null, role: 'affected' }])
      } else {
        setError(t('ci.notFound', { defaultValue: 'Configuration item {{ref}} was not found in the Entity Registry.', ref: initialCi }))
      }
    })
  }, [initialCi, t])

  useEffect(() => {
    // Reset type-specific choices when the type changes; keep title/description.
    setForm((f) => ({ title: f.title, description: f.description, impact: rt?.priority?.defaults.impact, urgency: rt?.priority?.defaults.urgency }))
    setCustom({})
  }, [type, rt])

  async function submit() {
    if (!rt) return
    setBusy(true)
    setError(null)
    try {
      const tags = String(form.tags ?? '').split(',').map((s) => s.trim()).filter(Boolean)
      const cleanCustom = Object.fromEntries(Object.entries(custom).filter(([, v]) => v !== null && v !== '' && v !== undefined))
      const res = await itsmApi.create({
        record_type: type,
        title: form.title, description: form.description || null,
        subtype: form.subtype || null, category: form.category || null,
        impact: form.impact ?? null, urgency: form.urgency ?? null,
        assignment_group: form.assignment_group || null, assignee: form.assignee || null,
        requester: form.requester || null, tags, custom_fields: cleanCustom,
        ci_links: ciLinks,
      })
      onCreated(res.record.uuid)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const prio = rt?.priority
  const derived = prio && form.impact != null && form.urgency != null ? prio.matrix[`${form.impact}-${form.urgency}`] : null

  return (
    <div className="lx-card" style={{ padding: '1.25rem', maxWidth: 860 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '1rem' }}>
        <Button label={t('common.back', { defaultValue: 'Back' })} icon="arrow_back" variant="ghost" onClick={onCancel} />
        <h2 style={{ margin: 0, fontSize: '1.05rem', fontWeight: 700, color: 'var(--lx-text)' }}>{t('create.title', { defaultValue: 'New record' })}</h2>
      </div>
      {error && <ErrorBox msg={error} />}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: '0.75rem' }}>
        <Labeled label={t('field.type', { defaultValue: 'Type' })}>
          <select className="lx-select" value={type} onChange={(e) => setType(e.target.value)}>
            {catalog?.record_types.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
          </select>
        </Labeled>
        {rt && rt.subtypes.length > 0 && (
          <Labeled label={t('field.subtype', { defaultValue: 'Subtype' })}>
            <select className="lx-select" value={String(form.subtype ?? '')} onChange={(e) => set('subtype', e.target.value)}>
              <option value="">—</option>
              {rt.subtypes.map((o) => <option key={String(o.id)} value={String(o.id)}>{o.label}</option>)}
            </select>
          </Labeled>
        )}
        {rt && rt.categories.length > 0 && (
          <Labeled label={t('field.category', { defaultValue: 'Category' })}>
            <select className="lx-select" value={String(form.category ?? '')} onChange={(e) => set('category', e.target.value)}>
              <option value="">—</option>
              {flattenCategories(rt).map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
          </Labeled>
        )}
        <div style={{ gridColumn: '1 / -1' }}>
          <Labeled label={t('field.title', { defaultValue: 'Title' })}>
            <input className="lx-input" value={String(form.title ?? '')} onChange={(e) => set('title', e.target.value)} />
          </Labeled>
        </div>
        <div style={{ gridColumn: '1 / -1' }}>
          <Labeled label={t('field.description', { defaultValue: 'Description' })}>
            <textarea className="lx-textarea" style={{ minHeight: 100 }} value={String(form.description ?? '')} onChange={(e) => set('description', e.target.value)} />
          </Labeled>
        </div>
        {prio && (
          <>
            <Labeled label={t('field.impact', { defaultValue: 'Impact' })}>
              <select className="lx-select" value={String(form.impact ?? '')} onChange={(e) => set('impact', Number(e.target.value))}>
                {prio.impact.map((o) => <option key={String(o.id)} value={String(o.id)}>{o.id} — {o.label}</option>)}
              </select>
            </Labeled>
            <Labeled label={t('field.urgency', { defaultValue: 'Urgency' })} hint={derived ? t('create.derivedPriority', { defaultValue: 'Priority: {{p}}', p: derived }) : undefined}>
              <select className="lx-select" value={String(form.urgency ?? '')} onChange={(e) => set('urgency', Number(e.target.value))}>
                {prio.urgency.map((o) => <option key={String(o.id)} value={String(o.id)}>{o.id} — {o.label}</option>)}
              </select>
            </Labeled>
          </>
        )}
        {rt && rt.assignment_groups.length > 0 && (
          <Labeled label={t('field.assignment_group', { defaultValue: 'Assignment group' })}>
            <select className="lx-select" value={String(form.assignment_group ?? '')} onChange={(e) => set('assignment_group', e.target.value)}>
              <option value="">—</option>
              {rt.assignment_groups.map((g) => <option key={g.id} value={g.id}>{g.label}</option>)}
            </select>
          </Labeled>
        )}
        <Labeled label={t('field.assignee', { defaultValue: 'Assignee' })}>
          <input className="lx-input" value={String(form.assignee ?? '')} onChange={(e) => set('assignee', e.target.value)} />
        </Labeled>
        <Labeled label={t('field.requester', { defaultValue: 'Requester' })}>
          <input className="lx-input" value={String(form.requester ?? '')} onChange={(e) => set('requester', e.target.value)} placeholder={t('create.requesterHint', { defaultValue: 'defaults to you' })} />
        </Labeled>
        <Labeled label={t('field.tags', { defaultValue: 'Tags (comma separated)' })}>
          <input className="lx-input" value={String(form.tags ?? '')} onChange={(e) => set('tags', e.target.value)} />
        </Labeled>
      </div>

      {rt && rt.fields.length > 0 && (
        <>
          <div className="lx-section-title" style={{ marginTop: '1rem' }}>{t('create.customFields', { defaultValue: 'Additional fields' })}</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: '0.75rem' }}>
            {rt.fields.map((f) => (
              <div key={f.id} style={f.multiline ? { gridColumn: '1 / -1' } : undefined}>
                <Labeled label={f.label} hint={f.description || undefined}>
                  <FieldInput field={f} value={custom[f.id]} onChange={(v) => setCustom((c) => ({ ...c, [f.id]: v }))} />
                </Labeled>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="lx-section-title" style={{ marginTop: '1rem' }}>{t('ci.title', { defaultValue: 'Configuration items' })}</div>
      <CiPicker roles={catalog?.ci_link_roles ?? ['affected']} linked={ciLinks} onPick={(e) => setCiLinks((l) => l.some((x) => x.entity_uuid === e.entity_uuid && x.role === e.role) ? l : [...l, e])} />
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.3rem', marginTop: '0.4rem' }}>
        {ciLinks.map((c) => (
          <span key={`${c.entity_uuid}:${c.role}`} className="lx-badge lx-badge--muted">
            {t(`ci.role.${c.role}`, { defaultValue: c.role })}: {c.display_name ?? c.entity_uuid.slice(0, 8)}
            <button className="lx-icon-btn" onClick={() => setCiLinks((l) => l.filter((x) => x !== c))}><span className="material-icons" style={{ fontSize: 13 }}>close</span></button>
          </span>
        ))}
      </div>

      <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end', marginTop: '1.25rem' }}>
        <Button label={t('common.cancel', { defaultValue: 'Cancel' })} onClick={onCancel} />
        <Button label={t('create.submit', { defaultValue: 'Create' })} icon="check" variant="primary" disabled={busy || !String(form.title ?? '').trim()} onClick={() => void submit()} />
      </div>
    </div>
  )
}

function TransitionModal({ record, rt, toState, onDone, onClose }: {
  record: RecordDetail
  rt: RecordTypeDef
  toState: string
  onDone: (r: RecordDetail) => void
  onClose: () => void
}) {
  const { t } = useTranslation('itsm')
  const target = rt.states.find((s) => s.id === toState)
  const required = target?.requires ?? []
  const coreReq = required.filter((r) => ['assignee', 'assignment_group', 'category', 'subtype', 'description'].includes(r))
  const customReq = required.map((r) => rt.fields.find((f) => f.id === r)).filter((f): f is FieldDef => Boolean(f))
  const [fields, setFields] = useState<Record<string, unknown>>(() => {
    const init: Record<string, unknown> = {}
    for (const f of customReq) init[f.id] = record.custom_fields[f.id] ?? null
    for (const c of coreReq) init[c] = (record as unknown as Record<string, unknown>)[c] ?? ''
    return init
  })
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function go() {
    setBusy(true)
    setError(null)
    try {
      const clean = Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== null && v !== '' && v !== undefined))
      const res = await itsmApi.transition(record.uuid, { to_state: toState, fields: clean, note: note.trim() || undefined, version: record.version })
      onDone(res.record)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={t('transition.title', { defaultValue: '{{number}} → {{state}}', number: record.number, state: target?.label ?? toState })} onClose={onClose}>
      {error && <ErrorBox msg={error} />}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
        {required.length > 0 && <p style={{ margin: 0, fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>{t('transition.requires', { defaultValue: 'This state requires:' })}</p>}
        {coreReq.map((c) => (
          <Labeled key={c} label={t(`field.${c}`, { defaultValue: c })}>
            {c === 'assignment_group' ? (
              <select className="lx-select" value={String(fields[c] ?? '')} onChange={(e) => setFields((f) => ({ ...f, [c]: e.target.value }))}>
                <option value="">—</option>
                {rt.assignment_groups.map((g) => <option key={g.id} value={g.id}>{g.label}</option>)}
              </select>
            ) : c === 'description' ? (
              <textarea className="lx-textarea" value={String(fields[c] ?? '')} onChange={(e) => setFields((f) => ({ ...f, [c]: e.target.value }))} />
            ) : (
              <input className="lx-input" value={String(fields[c] ?? '')} onChange={(e) => setFields((f) => ({ ...f, [c]: e.target.value }))} />
            )}
          </Labeled>
        ))}
        {customReq.map((f) => (
          <Labeled key={f.id} label={f.label} hint={f.description || undefined}>
            <FieldInput field={f} value={fields[f.id]} onChange={(v) => setFields((x) => ({ ...x, [f.id]: v }))} />
          </Labeled>
        ))}
        <Labeled label={t('transition.note', { defaultValue: 'Note (journal)' })}>
          <textarea className="lx-textarea" value={note} onChange={(e) => setNote(e.target.value)} />
        </Labeled>
        <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
          <Button label={t('common.cancel', { defaultValue: 'Cancel' })} onClick={onClose} />
          <Button label={target?.label ?? toState} icon="arrow_forward" variant="primary" disabled={busy} onClick={() => void go()} />
        </div>
      </div>
    </Modal>
  )
}

// `recordRef`, not `ref`: React reserves the `ref` prop and a string there crashes the render.
function RecordDetailView({ recordRef, catalog, onBack, onOpen, onChanged, onFilterCi }: {
  recordRef: string
  catalog: Catalog | null
  onBack: () => void
  onOpen: (ref: string) => void
  onChanged: () => void
  onFilterCi?: (ci: string) => void
}) {
  const { t } = useTranslation('itsm')
  const [record, setRecord] = useState<RecordDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [transitionTo, setTransitionTo] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [edit, setEdit] = useState<Record<string, unknown>>({})
  const [journalKind, setJournalKind] = useState<'comment' | 'work_note'>('work_note')
  const [journalText, setJournalText] = useState('')
  const [linkTo, setLinkTo] = useState('')
  const [linkRel, setLinkRel] = useState('related_to')
  const fileRef = useRef<HTMLInputElement>(null)
  const rt = record ? typeOf(catalog, record.record_type) : null

  const load = useCallback(async () => {
    setError(null)
    try {
      setRecord((await itsmApi.get(recordRef)).record)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [recordRef])
  useEffect(() => { void load() }, [load])

  async function run(fn: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await load()
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  function startEdit() {
    if (!record) return
    setEdit({
      title: record.title, description: record.description ?? '', impact: record.impact, urgency: record.urgency,
      subtype: record.subtype ?? '', category: record.category ?? '', assignment_group: record.assignment_group ?? '',
      assignee: record.assignee ?? '', requester: record.requester ?? '', tags: record.tags.join(', '),
    })
    setEditing(true)
  }

  async function saveEdit() {
    if (!record) return
    await run(async () => {
      await itsmApi.patch(record.uuid, {
        version: record.version,
        title: edit.title, description: edit.description || null,
        impact: edit.impact, urgency: edit.urgency,
        subtype: edit.subtype || null, category: edit.category || null,
        assignment_group: edit.assignment_group || null, assignee: edit.assignee || null,
        requester: edit.requester || null,
        tags: String(edit.tags ?? '').split(',').map((s) => s.trim()).filter(Boolean),
      })
      setEditing(false)
    })
  }

  const journalIcon = (kind: string) =>
    kind === 'comment' ? 'chat' : kind === 'work_note' ? 'engineering' : kind === 'state_change' ? 'swap_horiz'
    : kind === 'field_change' ? 'edit' : kind === 'link' ? 'link' : kind === 'attachment' ? 'attach_file' : 'info'

  const describeJournal = (j: RecordDetail['journal'][number]): string => {
    const d = j.data as Record<string, unknown>
    if (j.kind === 'state_change') return `${String(d.from)} → ${String(d.to)}`
    if (j.kind === 'field_change') {
      const ch = (d.changes ?? {}) as Record<string, { old: unknown; new: unknown }>
      return Object.entries(ch).map(([k, v]) => `${k}: ${String(v.old ?? '—')} → ${String(v.new ?? '—')}`).join(' · ')
    }
    if (j.kind === 'link') return `${String(d.action)} ${String(d.record ?? d.display_name ?? d.ci ?? '')} (${String(d.relation ?? d.role ?? '')})`
    if (j.kind === 'attachment') return `${String(d.action)} ${String(d.filename)}`
    if (j.kind === 'system') return String(j.body ?? '')
    return ''
  }

  if (!record && !error) return <div className="lx-empty"><span className="lx-spinner" /></div>

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.75rem', flexWrap: 'wrap' }}>
        <Button label={t('common.back', { defaultValue: 'Back' })} icon="arrow_back" variant="ghost" onClick={onBack} />
        {record && (
          <>
            <span className="lx-mono" style={{ fontSize: '0.85rem', color: 'var(--lx-text-muted)' }}>{record.number}</span>
            <h2 style={{ margin: 0, fontSize: '1.05rem', fontWeight: 700, color: 'var(--lx-text)' }}>{record.title}</h2>
            <TypeBadge type={record.record_type} catalog={catalog} />
            <StateBadge record={record} catalog={catalog} />
            <PriorityBadge record={record} catalog={catalog} />
            {record.sync_state && <span className="lx-badge lx-badge--paused">{record.sync_state}</span>}
            <span style={{ flex: 1 }} />
            {record.allowed_transitions.map((s) => (
              <Button key={s} label={rt?.states.find((x) => x.id === s)?.label ?? s} icon="arrow_forward" variant="primary" disabled={busy} onClick={() => setTransitionTo(s)} />
            ))}
          </>
        )}
      </div>
      {error && <ErrorBox msg={error} />}
      {record && rt && (
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 2fr) minmax(300px, 1fr)', gap: '1rem', alignItems: 'start' }}>
          {/* Left column: description + journal */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            <div className="lx-card" style={{ padding: '1rem' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div className="lx-section-title">{t('detail.details', { defaultValue: 'Details' })}</div>
                {!editing && record.allowed_transitions.length > 0 && <Button label={t('common.edit', { defaultValue: 'Edit' })} icon="edit" variant="ghost" onClick={startEdit} />}
              </div>
              {!editing ? (
                <>
                  <p style={{ whiteSpace: 'pre-wrap', fontSize: '0.85rem', color: 'var(--lx-text)', margin: '0.25rem 0 0.75rem' }}>{record.description || <span style={{ color: 'var(--lx-text-muted)' }}>—</span>}</p>
                  <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr auto 1fr', gap: '0.3rem 0.75rem', fontSize: '0.8rem' }}>
                    <span className="lx-label">{t('field.subtype', { defaultValue: 'Subtype' })}</span><span>{rt.subtypes.find((s) => String(s.id) === record.subtype)?.label ?? record.subtype ?? '—'}</span>
                    <span className="lx-label">{t('field.category', { defaultValue: 'Category' })}</span><span>{record.category ?? '—'}</span>
                    <span className="lx-label">{t('field.impact', { defaultValue: 'Impact' })}</span><span>{record.impact ?? '—'}</span>
                    <span className="lx-label">{t('field.urgency', { defaultValue: 'Urgency' })}</span><span>{record.urgency ?? '—'}</span>
                    <span className="lx-label">{t('field.assignment_group', { defaultValue: 'Group' })}</span><span>{rt.assignment_groups.find((g) => g.id === record.assignment_group)?.label ?? record.assignment_group ?? '—'}</span>
                    <span className="lx-label">{t('field.assignee', { defaultValue: 'Assignee' })}</span><span>{record.assignee ?? '—'}</span>
                    <span className="lx-label">{t('field.requester', { defaultValue: 'Requester' })}</span><span>{record.requester ?? '—'}</span>
                    <span className="lx-label">{t('field.tags', { defaultValue: 'Tags' })}</span><span>{record.tags.join(', ') || '—'}</span>
                    <span className="lx-label">{t('field.opened', { defaultValue: 'Opened' })}</span><span>{fmt(record.opened_at)}</span>
                    <span className="lx-label">{t('field.resolved', { defaultValue: 'Resolved' })}</span><span>{fmt(record.resolved_at)}</span>
                    <span className="lx-label">{t('field.closed', { defaultValue: 'Closed' })}</span><span>{fmt(record.closed_at)}</span>
                    <span className="lx-label">{t('field.version', { defaultValue: 'Version' })}</span><span>{record.version}</span>
                  </div>
                  {Object.keys(record.custom_fields).length > 0 && (
                    <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '0.3rem 0.75rem', fontSize: '0.8rem', marginTop: '0.75rem', borderTop: '1px solid var(--lx-border-soft)', paddingTop: '0.5rem' }}>
                      {Object.entries(record.custom_fields).map(([k, v]) => {
                        const f = rt.fields.find((x) => x.id === k)
                        const label = f?.kind === 'select' ? f.options.find((o) => String(o.id) === String(v))?.label ?? String(v) : typeof v === 'boolean' ? (v ? '✓' : '✗') : String(v ?? '—')
                        return (<React.Fragment key={k}><span className="lx-label">{f?.label ?? k}</span><span style={{ whiteSpace: 'pre-wrap' }}>{label}</span></React.Fragment>)
                      })}
                    </div>
                  )}
                </>
              ) : (
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '0.6rem', marginTop: '0.5rem' }}>
                  <div style={{ gridColumn: '1 / -1' }}><Labeled label={t('field.title', { defaultValue: 'Title' })}><input className="lx-input" value={String(edit.title ?? '')} onChange={(e) => setEdit((x) => ({ ...x, title: e.target.value }))} /></Labeled></div>
                  <div style={{ gridColumn: '1 / -1' }}><Labeled label={t('field.description', { defaultValue: 'Description' })}><textarea className="lx-textarea" style={{ minHeight: 90 }} value={String(edit.description ?? '')} onChange={(e) => setEdit((x) => ({ ...x, description: e.target.value }))} /></Labeled></div>
                  {rt.subtypes.length > 0 && <Labeled label={t('field.subtype', { defaultValue: 'Subtype' })}><select className="lx-select" value={String(edit.subtype ?? '')} onChange={(e) => setEdit((x) => ({ ...x, subtype: e.target.value }))}><option value="">—</option>{rt.subtypes.map((o) => <option key={String(o.id)} value={String(o.id)}>{o.label}</option>)}</select></Labeled>}
                  {rt.categories.length > 0 && <Labeled label={t('field.category', { defaultValue: 'Category' })}><select className="lx-select" value={String(edit.category ?? '')} onChange={(e) => setEdit((x) => ({ ...x, category: e.target.value }))}><option value="">—</option>{flattenCategories(rt).map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}</select></Labeled>}
                  {rt.priority && <Labeled label={t('field.impact', { defaultValue: 'Impact' })}><select className="lx-select" value={String(edit.impact ?? '')} onChange={(e) => setEdit((x) => ({ ...x, impact: Number(e.target.value) }))}>{rt.priority.impact.map((o) => <option key={String(o.id)} value={String(o.id)}>{o.id} — {o.label}</option>)}</select></Labeled>}
                  {rt.priority && <Labeled label={t('field.urgency', { defaultValue: 'Urgency' })}><select className="lx-select" value={String(edit.urgency ?? '')} onChange={(e) => setEdit((x) => ({ ...x, urgency: Number(e.target.value) }))}>{rt.priority.urgency.map((o) => <option key={String(o.id)} value={String(o.id)}>{o.id} — {o.label}</option>)}</select></Labeled>}
                  {rt.assignment_groups.length > 0 && <Labeled label={t('field.assignment_group', { defaultValue: 'Group' })}><select className="lx-select" value={String(edit.assignment_group ?? '')} onChange={(e) => setEdit((x) => ({ ...x, assignment_group: e.target.value }))}><option value="">—</option>{rt.assignment_groups.map((g) => <option key={g.id} value={g.id}>{g.label}</option>)}</select></Labeled>}
                  <Labeled label={t('field.assignee', { defaultValue: 'Assignee' })}><input className="lx-input" value={String(edit.assignee ?? '')} onChange={(e) => setEdit((x) => ({ ...x, assignee: e.target.value }))} /></Labeled>
                  <Labeled label={t('field.requester', { defaultValue: 'Requester' })}><input className="lx-input" value={String(edit.requester ?? '')} onChange={(e) => setEdit((x) => ({ ...x, requester: e.target.value }))} /></Labeled>
                  <Labeled label={t('field.tags', { defaultValue: 'Tags' })}><input className="lx-input" value={String(edit.tags ?? '')} onChange={(e) => setEdit((x) => ({ ...x, tags: e.target.value }))} /></Labeled>
                  <div style={{ gridColumn: '1 / -1', display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
                    <Button label={t('common.cancel', { defaultValue: 'Cancel' })} onClick={() => setEditing(false)} />
                    <Button label={t('common.save', { defaultValue: 'Save' })} icon="check" variant="primary" disabled={busy} onClick={() => void saveEdit()} />
                  </div>
                </div>
              )}
            </div>

            <div className="lx-card" style={{ padding: '1rem' }}>
              <div className="lx-section-title">{t('journal.title', { defaultValue: 'Timeline' })}</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', margin: '0.5rem 0 0.75rem' }}>
                {record.journal.map((j) => (
                  <div key={j.id} style={{ display: 'flex', gap: '0.6rem', fontSize: '0.8rem', padding: '0.4rem 0', borderBottom: '1px solid var(--lx-border-soft)' }}>
                    <span className="material-icons" style={{ fontSize: 16, color: j.kind === 'comment' ? 'var(--lx-accent)' : 'var(--lx-text-muted)', marginTop: 2 }}>{journalIcon(j.kind)}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ color: 'var(--lx-text-muted)', fontSize: '0.7rem' }}>
                        <b style={{ color: 'var(--lx-text)' }}>{j.author ?? 'system'}</b> · {t(`journal.kind.${j.kind}`, { defaultValue: j.kind })} · {fmt(j.created_at)}
                      </div>
                      {j.body && <div style={{ whiteSpace: 'pre-wrap', color: 'var(--lx-text)' }}>{j.body}</div>}
                      {describeJournal(j) && j.kind !== 'system' && <div className="lx-mono" style={{ fontSize: '0.7rem', color: 'var(--lx-text-muted)' }}>{describeJournal(j)}</div>}
                    </div>
                  </div>
                ))}
              </div>
              <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'flex-start' }}>
                <select className="lx-select" style={{ flex: '0 0 150px', width: 'auto' }} value={journalKind} onChange={(e) => setJournalKind(e.target.value as 'comment' | 'work_note')}>
                  <option value="work_note">{t('journal.kind.work_note', { defaultValue: 'Work note' })}</option>
                  <option value="comment">{t('journal.kind.comment', { defaultValue: 'Comment' })}</option>
                </select>
                <textarea className="lx-textarea" style={{ flex: '1 1 auto', minWidth: 0, minHeight: 60 }} placeholder={t('journal.placeholder', { defaultValue: 'Add to the timeline…' })} value={journalText} onChange={(e) => setJournalText(e.target.value)} />
                <Button label={t('journal.add', { defaultValue: 'Add' })} icon="send" variant="primary" disabled={busy || !journalText.trim()} onClick={() => void run(async () => { await itsmApi.addJournal(record.uuid, journalKind, journalText); setJournalText('') })} />
              </div>
            </div>
          </div>

          {/* Right column: CIs, links, attachments */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            <div className="lx-card" style={{ padding: '1rem' }}>
              <div className="lx-section-title">{t('ci.title', { defaultValue: 'Configuration items' })}</div>
              {record.ci_links.length === 0 && <div style={{ fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>—</div>}
              {record.ci_links.map((c) => (
                <div key={c.id} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.8rem', padding: '0.25rem 0' }}>
                  <span className="lx-badge lx-badge--muted">{t(`ci.role.${c.role}`, { defaultValue: c.role })}</span>
                  <a href={registryApi.deepLink(c.entity_uuid)} onClick={(e) => { e.preventDefault(); window.history.pushState({}, '', registryApi.deepLink(c.entity_uuid)); window.dispatchEvent(new PopStateEvent('popstate')) }} style={{ fontWeight: 600, color: 'var(--lx-accent)', flex: 1 }}>
                    {c.display_name ?? c.entity_uuid.slice(0, 8)}
                  </a>
                  {c.entity_type && <span style={{ color: 'var(--lx-text-muted)', fontSize: '0.7rem' }}>{c.entity_type}</span>}
                  {onFilterCi && (
                    <button className="lx-icon-btn" title={t('ci.recordsForCi', { defaultValue: 'Show all records for this configuration item' })} onClick={() => onFilterCi(c.entity_uuid)}>
                      <span className="material-icons" style={{ fontSize: 15 }}>filter_list</span>
                    </button>
                  )}
                  <button className="lx-icon-btn lx-icon-btn--danger" disabled={busy} onClick={() => void run(() => itsmApi.unlinkCi(record.uuid, c.role, c.entity_uuid))}><span className="material-icons" style={{ fontSize: 15 }}>link_off</span></button>
                </div>
              ))}
              <div style={{ marginTop: '0.5rem' }}>
                <CiPicker roles={catalog?.ci_link_roles ?? ['affected']} linked={record.ci_links} onPick={(e) => void run(() => itsmApi.linkCi(record.uuid, e))} />
              </div>
            </div>

            <div className="lx-card" style={{ padding: '1rem' }}>
              <div className="lx-section-title">{t('links.title', { defaultValue: 'Related records' })}</div>
              {record.links.length === 0 && <div style={{ fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>—</div>}
              {record.links.map((l) => (
                <div key={l.id} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.8rem', padding: '0.25rem 0' }}>
                  <span style={{ color: 'var(--lx-text-muted)' }}>{t(`links.rel.${l.relation_from_here}`, { defaultValue: l.relation_from_here.replace(/_/g, ' ') })}</span>
                  <a onClick={() => onOpen(l.other_uuid)} style={{ cursor: 'pointer', fontWeight: 600, color: 'var(--lx-accent)', flex: 1 }}>
                    <span className="lx-mono">{l.other?.number}</span> {l.other?.title}
                  </a>
                  {l.other && <span className="lx-badge lx-badge--muted">{l.other.state}</span>}
                  <button className="lx-icon-btn lx-icon-btn--danger" disabled={busy} onClick={() => void run(() => itsmApi.unlinkRecord(record.uuid, l.relation_from_here, l.other_uuid))}><span className="material-icons" style={{ fontSize: 15 }}>link_off</span></button>
                </div>
              ))}
              <div style={{ display: 'flex', gap: '0.4rem', marginTop: '0.5rem' }}>
                <select className="lx-select" style={{ flex: '0 0 170px', width: 'auto' }} value={linkRel} onChange={(e) => setLinkRel(e.target.value)}>
                  {Object.keys(catalog?.record_link_relations ?? {}).map((r) => <option key={r} value={r}>{t(`links.rel.${r}`, { defaultValue: r.replace(/_/g, ' ') })}</option>)}
                </select>
                <input className="lx-input lx-mono" style={{ flex: 1 }} placeholder={t('links.placeholder', { defaultValue: 'INC0000042' })} value={linkTo} onChange={(e) => setLinkTo(e.target.value)} />
                <Button label={t('common.add', { defaultValue: 'Add' })} icon="link" disabled={busy || !linkTo.trim()} onClick={() => void run(async () => { await itsmApi.linkRecord(record.uuid, linkTo.trim(), linkRel); setLinkTo('') })} />
              </div>
            </div>

            <div className="lx-card" style={{ padding: '1rem' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div className="lx-section-title">{t('attachments.title', { defaultValue: 'Attachments' })}</div>
                <input ref={fileRef} type="file" style={{ display: 'none' }} onChange={(e) => { const f = e.target.files?.[0]; if (f) void run(() => itsmApi.upload(record.uuid, f)); e.target.value = '' }} />
                <Button label={t('attachments.upload', { defaultValue: 'Upload' })} icon="upload" variant="ghost" disabled={busy} onClick={() => fileRef.current?.click()} />
              </div>
              {record.attachments.length === 0 && <div style={{ fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>—</div>}
              {record.attachments.map((a: AttachmentMeta) => (
                <div key={a.uuid} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.8rem', padding: '0.25rem 0' }}>
                  <span className="material-icons" style={{ fontSize: 16, color: 'var(--lx-text-muted)' }}>attach_file</span>
                  <a onClick={() => void itsmApi.download(record.uuid, a)} style={{ cursor: 'pointer', fontWeight: 600, color: 'var(--lx-accent)', flex: 1, wordBreak: 'break-all' }}>{a.filename}</a>
                  <span style={{ color: 'var(--lx-text-muted)', fontSize: '0.7rem', whiteSpace: 'nowrap' }}>{fmtBytes(a.size)}</span>
                  <button className="lx-icon-btn lx-icon-btn--danger" disabled={busy} onClick={() => void run(() => itsmApi.deleteAttachment(record.uuid, a.uuid))}><span className="material-icons" style={{ fontSize: 15 }}>delete_outline</span></button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
      {record && rt && transitionTo && (
        <TransitionModal record={record} rt={rt} toState={transitionTo} onClose={() => setTransitionTo(null)}
          onDone={(r) => { setTransitionTo(null); setRecord(r); onChanged() }} />
      )}
    </>
  )
}

function CatalogView({ catalog, onReload, onBack }: { catalog: Catalog | null; onReload: () => Promise<void>; onBack: () => void }) {
  const { t } = useTranslation('itsm')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.75rem' }}>
        <Button label={t('common.back', { defaultValue: 'Back' })} icon="arrow_back" variant="ghost" onClick={onBack} />
        <h2 style={{ margin: 0, fontSize: '1.05rem', fontWeight: 700, color: 'var(--lx-text)' }}>{t('catalog.title', { defaultValue: 'Process catalog' })}</h2>
        <span style={{ flex: 1 }} />
        <Button label={t('catalog.reload', { defaultValue: 'Reload from disk' })} icon="refresh" disabled={busy} onClick={() => { setBusy(true); setError(null); onReload().catch((e) => setError(e instanceof Error ? e.message : String(e))).finally(() => setBusy(false)) }} />
      </div>
      <p style={{ fontSize: '0.8rem', color: 'var(--lx-text-muted)', marginTop: 0 }}>{t('catalog.hint', { defaultValue: 'Record types are defined in catalog/record_types/*.yml. Edit the YAML (or point the plugin at your own catalog dir in its settings) and reload.' })}</p>
      {error && <ErrorBox msg={error} />}
      {catalog?.record_types.map((rt) => (
        <div key={rt.id} className="lx-card" style={{ padding: '1rem', marginBottom: '0.75rem' }}>
          <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
            <span className="material-icons" style={{ fontSize: 20, color: 'var(--lx-accent)' }}>{rt.icon}</span>
            <b style={{ color: 'var(--lx-text)' }}>{rt.label}</b>
            <span className="lx-mono lx-badge lx-badge--muted">{rt.prefix}</span>
            <span className="lx-badge lx-badge--muted">{rt.mode}</span>
            <span style={{ fontSize: '0.78rem', color: 'var(--lx-text-muted)' }}>{rt.description}</span>
          </div>
          <table className="lx-table" style={{ marginTop: '0.5rem' }}>
            <thead><tr><th>{t('catalog.state', { defaultValue: 'State' })}</th><th>{t('catalog.kind', { defaultValue: 'Kind' })}</th><th>{t('catalog.transitions', { defaultValue: 'Transitions to' })}</th><th>{t('catalog.requires', { defaultValue: 'Requires' })}</th></tr></thead>
            <tbody>
              {rt.states.map((s) => (
                <tr key={s.id}>
                  <td style={{ fontWeight: 600 }}>{s.label} {s.initial && <span className="lx-badge lx-badge--accent">initial</span>}{s.final && <span className="lx-badge lx-badge--muted">final</span>}</td>
                  <td style={{ fontSize: '0.78rem' }}>{s.kind}</td>
                  <td style={{ fontSize: '0.78rem' }}>{(rt.transitions[s.id] ?? []).map((x) => rt.states.find((y) => y.id === x)?.label ?? x).join(', ') || '—'}</td>
                  <td className="lx-mono" style={{ fontSize: '0.72rem' }}>{s.requires.join(', ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ fontSize: '0.75rem', color: 'var(--lx-text-muted)', marginTop: '0.4rem' }}>
            {t('catalog.summary', { defaultValue: '{{subtypes}} subtypes · {{fields}} custom fields · {{groups}} groups', subtypes: rt.subtypes.length, fields: rt.fields.length, groups: rt.assignment_groups.length })}
          </div>
        </div>
      ))}
    </>
  )
}

// ─── Root ─────────────────────────────────────────────────────────────────────

export default function PluginApp() {
  const { t } = useTranslation('itsm')
  const { catalog, error: catalogError, setCatalog } = useCatalog()
  const [stats, setStats] = useState<Stats | null>(null)
  // Deep links: /itsm?record=<uuid|number> opens a record,
  // /itsm?new=<type>&ci=<uuid> opens the create form with that CI attached
  // (Server Manager / Entity Registry "report incident"), /itsm?ci=<uuid>
  // lists the records referencing a CI.
  const initial = useMemo<View>(() => {
    const p = new URLSearchParams(window.location.search)
    const ref = p.get('record')
    const ci = p.get('ci') ?? undefined
    const newType = p.get('new')
    if (newType !== null) return { kind: 'create', type: newType || undefined, ci }
    if (ref) return { kind: 'detail', ref }
    return ci ? { kind: 'list', ci } : { kind: 'list' }
  }, [])
  const [view, setView] = useState<View>(initial)

  const refreshStats = useCallback(() => { itsmApi.stats().then(setStats).catch(() => setStats(null)) }, [])
  useEffect(() => { refreshStats() }, [refreshStats, view])

  const open = (ref: string) => setView({ kind: 'detail', ref })

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: '1.5rem 1.5rem 3rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ margin: 0, fontSize: '1.25rem', fontWeight: 700, letterSpacing: '-0.01em', color: 'var(--lx-text)' }}>{t('title', { defaultValue: 'Service Management' })}</h1>
          <p style={{ margin: '0.25rem 0 0', fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>{t('subtitle', { defaultValue: 'Incidents, changes, problems and requests — one process engine, defined in the catalog.' })}</p>
        </div>
        {view.kind === 'list' && (
          <div style={{ display: 'flex', gap: '0.5rem' }}>
            <Button label={t('catalog.title', { defaultValue: 'Process catalog' })} icon="account_tree" onClick={() => setView({ kind: 'catalog' })} />
            <Button label={t('create.new', { defaultValue: 'New record' })} icon="add" variant="primary" onClick={() => setView({ kind: 'create' })} />
          </div>
        )}
      </div>
      {catalogError && <ErrorBox msg={catalogError} />}
      <StatsStrip stats={stats} catalog={catalog} />
      {view.kind === 'list' && <RecordList key={view.ci ?? ''} catalog={catalog} onOpen={open} initialCi={view.ci} />}
      {view.kind === 'create' && <CreateForm catalog={catalog} initialType={view.type} initialCi={view.ci} onCreated={open} onCancel={() => setView({ kind: 'list' })} />}
      {view.kind === 'detail' && <RecordDetailView recordRef={view.ref} catalog={catalog} onBack={() => setView({ kind: 'list' })} onOpen={open} onChanged={refreshStats} onFilterCi={(ci) => setView({ kind: 'list', ci })} />}
      {view.kind === 'catalog' && <CatalogView catalog={catalog} onBack={() => setView({ kind: 'list' })} onReload={async () => { setCatalog(await itsmApi.reloadCatalog()) }} />}
    </div>
  )
}
