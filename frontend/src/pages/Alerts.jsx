import { useState } from 'react';
import { api, useApi } from '../api';
import { ExploreButton } from '../components/Grafana';
import { useApp } from '../ctx';
import { Badge, Card, Empty, ErrorNote, Modal, Seg, SevBadge, Toggle } from '../components/ui';
import { fmtAgo, fmtDateTime, fmtDuration, fmtValue } from '../format';

const UNITS = ['short', 'percent', 'bytes', 'seconds', 'ms', 'days', 'rps'];
const BLANK = { name: '', expr: '', datasource: 'prometheus', direction: 'above', warn: '', crit: '', for_seconds: 300, unit: 'short', summary: '', enabled: true };
const num = (v) => (v === '' || v == null ? null : Number(v));
const splitIds = (t) => t.split(',').map((x) => x.trim()).filter(Boolean);

function useServerName() {
  const { servers } = useApp();
  return (id) => servers.find((s) => s.id === id)?.name || id;
}

function ServerTag({ id }) {
  const { multi } = useApp();
  const name = useServerName();
  return multi ? <Badge kind="info">{name(id)}</Badge> : null;
}

export function RuleModal({ rule, initial, onClose, onSaved }) {
  const { servers, multi } = useApp();
  const [f, setF] = useState(rule ? { ...rule, warn: rule.warn ?? '', crit: rule.crit ?? '' } : { ...BLANK, ...initial });
  const [scope, setScope] = useState((rule?.servers || []).join(', '));
  const [err, setErr] = useState(null);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === 'number' ? e.target.value : e.target.value });
  const payload = () => ({ ...f, warn: num(f.warn), crit: num(f.crit), for_seconds: Number(f.for_seconds), servers: splitIds(scope) });

  const test = async () => {
    setErr(null); setBusy(true);
    try { setPreview(await api('/rules/test', { method: 'POST', body: payload() })); } catch (e) { setErr(e.message); setPreview(null); }
    setBusy(false);
  };
  const save = async () => {
    setErr(null); setBusy(true);
    try {
      await api(rule ? `/rules/${rule.id}` : '/rules', { method: rule ? 'PUT' : 'POST', body: payload() });
      onSaved();
    } catch (e) { setErr(e.message); }
    setBusy(false);
  };

  return (
    <Modal title={rule ? 'Edit alert rule' : 'New alert rule'} onClose={onClose} actions={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <ExploreButton expr={f.expr} datasource={f.datasource} />
      <button className="btn" onClick={test} disabled={busy || !f.expr}>Preview</button>
      <button className="btn primary" onClick={save} disabled={busy || !f.name || !f.expr}>Save</button>
    </>}>
      <label className="f" htmlFor="r-name">Name</label>
      <input id="r-name" type="text" value={f.name} onChange={set('name')} autoFocus />
      {(servers.some((s) => s.logs) || f.datasource === 'loki') && (
        <>
          <label className="f" htmlFor="r-ds">Data source</label>
          <select id="r-ds" value={f.datasource} onChange={set('datasource')}>
            <option value="prometheus">Prometheus (metrics, PromQL)</option>
            <option value="loki">Loki (logs, LogQL)</option>
          </select>
        </>
      )}
      <label className="f" htmlFor="r-expr">{f.datasource === 'loki' ? 'LogQL metric expression' : 'PromQL expression'}</label>
      <textarea id="r-expr" value={f.expr} onChange={set('expr')} spellCheck={false}
        placeholder={f.datasource === 'loki' ? 'sum by (container) (count_over_time({container=~".+"} |~ "(?i)error" [5m]))' : ''} />
      <div className="hint">
        {f.datasource === 'loki'
          ? 'Must return numbers, so wrap a log selector in count_over_time(...) or rate(...). One alert per resulting series.'
          : 'Returns one value per series; each series alerts on its own (e.g. per container or mountpoint).'}
      </div>
      <div className="row wrap" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 110 }}><label className="f" htmlFor="r-dir">Alert when</label>
          <select id="r-dir" value={f.direction} onChange={set('direction')}><option value="above">value is above</option><option value="below">value is below</option></select></div>
        <div style={{ flex: 1, minWidth: 90 }}><label className="f" htmlFor="r-warn">Warning at</label><input id="r-warn" type="number" step="any" value={f.warn} onChange={set('warn')} /></div>
        <div style={{ flex: 1, minWidth: 90 }}><label className="f" htmlFor="r-crit">Critical at</label><input id="r-crit" type="number" step="any" value={f.crit} onChange={set('crit')} /></div>
      </div>
      <div className="row wrap" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 110 }}><label className="f" htmlFor="r-for">For (seconds)</label><input id="r-for" type="number" min="0" value={f.for_seconds} onChange={set('for_seconds')} />
          <div className="hint">Must stay breached this long before alerting.</div></div>
        <div style={{ flex: 1, minWidth: 110 }}><label className="f" htmlFor="r-unit">Unit</label>
          <select id="r-unit" value={f.unit} onChange={set('unit')}>{UNITS.map((u) => <option key={u}>{u}</option>)}</select></div>
      </div>
      <label className="f" htmlFor="r-sum">What it means / what to do</label>
      <input id="r-sum" type="text" value={f.summary} onChange={set('summary')} />
      {multi && (
        <>
          <label className="f" htmlFor="r-scope">Only on servers</label>
          <input id="r-scope" type="text" value={scope} onChange={(e) => setScope(e.target.value)} placeholder="all servers" />
          <div className="hint">Comma-separated ids ({servers.map((s) => s.id).join(', ')}). Leave empty to evaluate on every server. Preview uses the server selected in the header.</div>
        </>
      )}
      <ErrorNote>{err}</ErrorNote>
      {preview && (
        <div className="notice" style={{ marginTop: 12 }}>
          {preview.count === 0 ? 'Query returned no series.' : `${preview.count} series. Right now:`}
          {preview.series.slice(0, 6).map((s) => <div key={s.series} className="mono">{s.series || '(all)'} = {fmtValue(s.value, f.unit)} {s.severity ? `→ ${s.severity}` : '→ ok'}</div>)}
        </div>
      )}
    </Modal>
  );
}

const ACK_CHOICES = [['1 hour', 1], ['4 hours', 4], ['24 hours', 24], ['Until it resolves', null]];

export function AckModal({ incident, onClose, onDone }) {
  const [note, setNote] = useState('');
  const [hours, setHours] = useState(4);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true); setErr(null);
    try { await api(`/incidents/${incident.id}/ack`, { method: 'POST', body: { hours, note } }); onDone(); }
    catch (e) { setErr(e.message); setBusy(false); }
  };
  return (
    <Modal title="Acknowledge alert" onClose={onClose} actions={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" onClick={save} disabled={busy}>Acknowledge</button>
    </>}>
      <div className="muted"><b>{incident.rule_name}</b>{incident.series && ` · ${incident.series}`}</div>
      <p className="hint" style={{ marginBottom: 0 }}>
        Tells your channels you are on it and pauses reminders for this alert. If it gets worse (warning to critical) or
        the time is up, you are notified again.
      </p>
      <label className="f" htmlFor="ack-note">Note (optional)</label>
      <input id="ack-note" type="text" value={note} maxLength={300} placeholder="e.g. restarting the worker" onChange={(e) => setNote(e.target.value)} autoFocus />
      <label className="f" htmlFor="ack-for">Pause reminders for</label>
      <select id="ack-for" value={hours === null ? 'null' : hours} onChange={(e) => setHours(e.target.value === 'null' ? null : Number(e.target.value))}>
        {ACK_CHOICES.map(([label, h]) => <option key={label} value={h === null ? 'null' : h}>{label}</option>)}
      </select>
      <div style={{ marginTop: 12 }}><ErrorNote>{err}</ErrorNote></div>
    </Modal>
  );
}

function AckBadge({ i }) {
  if (!i.acked_at) return null;
  const until = i.ack_until ? `until ${new Date(i.ack_until * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : 'until resolved';
  return <Badge kind="info" icon="check">acknowledged by {i.acked_by} · {until}</Badge>;
}

function Active({ server }) {
  const { can } = useApp();
  const [ackFor, setAckFor] = useState(null);
  const { data, error, reload } = useApi(`/incidents?status=firing${server ? `&server=${server}` : ''}`, { interval: 10000 });
  const silence = async (i, minutes) => { await api('/silences', { method: 'POST', body: { server: i.server, source_id: i.source_id, match: i.series || null, minutes, reason: 'Silenced from alerts page' } }); reload(); };
  const unack = async (i) => { await api(`/incidents/${i.id}/ack`, { method: 'DELETE' }); reload(); };
  if (!data) return error ? <ErrorNote>{error}</ErrorNote> : null;
  if (!data.length) return <Card><Empty title="Nothing is firing">All rules are inside their thresholds.</Empty></Card>;
  return (
    <>
    <Card flush>
      <div className="scroll"><table className="t">
        <thead><tr><th>Severity</th><th>Alert</th><th>Value</th><th>Since</th><th /></tr></thead>
        <tbody>{data.map((i) => (
          <tr key={i.id}>
            <td><SevBadge sev={i.severity} /></td>
            <td><a href={`#/incident/${i.id}`}><b>{i.rule_name}</b></a>{i.series && <span className="muted"> · {i.series}</span>} <ServerTag id={i.server} /><div className="muted" style={{ fontSize: 12.5 }}>{i.summary}</div></td>
            <td>{fmtValue(i.last_value, i.unit)}{i.threshold != null && <div className="muted" style={{ fontSize: 12 }}>threshold {fmtValue(i.threshold, i.unit)}</div>}</td>
            <td>{fmtAgo(i.started_at)}{i.silenced && <div><Badge>silenced</Badge></div>}<div><AckBadge i={i} /></div>{i.ack_note && <div className="muted" style={{ fontSize: 12 }}>“{i.ack_note}”</div>}</td>
            <td style={{ whiteSpace: 'nowrap' }}>{can.edit && <>
              {i.acked_at ? <button className="btn sm" onClick={() => unack(i)}>Un-acknowledge</button> : <button className="btn sm" onClick={() => setAckFor(i)}>Acknowledge</button>}{' '}
              <button className="btn sm" onClick={() => silence(i, 60)}>Silence 1h</button> <button className="btn sm" onClick={() => silence(i, 1440)}>24h</button></>}</td>
          </tr>
        ))}</tbody>
      </table></div>
    </Card>
    {ackFor && <AckModal incident={ackFor} onClose={() => setAckFor(null)} onDone={() => { setAckFor(null); reload(); }} />}
    </>
  );
}

const WINDOWS = { '1h': 1 / 24, '6h': 0.25, '24h': 1, '7d': 7, '14d': 14 };

/** One lane per alert; bars span start -> resolved (or now while still firing). */
function Timeline({ rows, days }) {
  const { multi } = useApp();
  const serverName = useServerName();
  const end = Date.now() / 1000;
  const start = end - days * 86400;
  const span = end - start;
  const lanes = new Map();
  rows.forEach((i) => {
    const key = `${multi ? `[${serverName(i.server)}] ` : ''}${i.rule_name}${i.series ? ` · ${i.series}` : ''}`;
    if (!lanes.has(key)) lanes.set(key, []);
    lanes.get(key).push(i);
  });
  const list = [...lanes.entries()].slice(0, 14);
  const parts = days <= 1 ? 6 : Math.min(Math.round(days), 7);
  const ticks = Array.from({ length: parts + 1 }, (_, n) => start + (span / parts) * n);
  const pct = (t) => `${Math.min(100, Math.max(0, ((t - start) / span) * 100))}%`;
  return (
    <div className="timeline" role="img" aria-label="Incident timeline">
      {list.map(([label, items]) => (
        <div key={label} className="tl-row">
          <div className="tl-label" title={label}>{label}</div>
          <div className="tl-track">
            {ticks.map((t, n) => <i key={n} className="tl-tick" style={{ left: pct(t) }} />)}
            {items.map((i) => {
              const from = Math.max(start, i.started_at);
              const to = i.resolved_at || end;
              return (
                <span key={i.id} className={`tl-bar ${i.severity === 'critical' ? 'crit' : 'warn'} ${i.resolved_at ? '' : 'open'}`}
                  style={{ left: pct(from), width: `max(5px, ${((to - from) / span) * 100}%)` }}
                  title={`${i.rule_name}: ${fmtDateTime(i.started_at)} → ${i.resolved_at ? fmtDateTime(i.resolved_at) : 'still firing'} (${fmtDuration(to - i.started_at)})`} />
              );
            })}
          </div>
        </div>
      ))}
      <div className="tl-row"><div className="tl-label" /><div className="tl-axis">{ticks.map((t, n) => <span key={n} style={{ left: pct(t) }}>{days <= 1 ? new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : new Date(t * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' })}</span>)}</div></div>
    </div>
  );
}

function History({ server }) {
  const [win, setWin] = useState('6h');
  const days = WINDOWS[win];
  const { data, error } = useApi(`/incidents?days=${days}&limit=200${server ? `&server=${server}` : ''}`, { interval: 30000 });
  if (!data) return error ? <ErrorNote>{error}</ErrorNote> : null;
  return (
    <div className="stack">
      <div className="row"><span className="muted">When each alert started and how long it lasted.</span>
        <span className="right"><Seg value={win} options={Object.keys(WINDOWS)} onChange={setWin} /></span></div>
      {!data.length ? <Card><Empty title={`No incidents in the last ${win}`}>Nothing has crossed a threshold in this window.</Empty></Card> : <>
        <Card title="Timeline" sub={`${data.length} incident${data.length === 1 ? '' : 's'}`}><Timeline rows={data} days={days} /></Card>
        <HistoryTable data={data} />
      </>}
    </div>
  );
}

function HistoryTable({ data }) {
  return (
    <Card flush><div className="scroll"><table className="t">
      <thead><tr><th>Status</th><th>Severity</th><th>Alert</th><th>Started</th><th>Duration</th><th>Peak</th></tr></thead>
      <tbody>{data.map((i) => (
        <tr key={i.id}>
          <td>{i.status === 'firing' ? <Badge kind="crit" icon="alert">Firing</Badge> : <Badge kind="ok" icon="check">Resolved</Badge>}</td>
          <td><SevBadge sev={i.severity} /></td>
          <td><a href={`#/incident/${i.id}`}><b>{i.rule_name}</b></a>{i.series && <span className="muted"> · {i.series}</span>} <ServerTag id={i.server} /></td>
          <td>{fmtDateTime(i.started_at)}</td>
          <td>{fmtDuration((i.resolved_at || Date.now() / 1000) - i.started_at)}</td>
          <td>{fmtValue(i.peak_value, i.unit)}</td>
        </tr>
      ))}</tbody>
    </table></div></Card>
  );
}

function Rules() {
  const { can } = useApp();
  const { data, error, reload } = useApi('/rules', { interval: 20000 });
  const [edit, setEdit] = useState(null);
  const toggle = async (r, enabled) => { await api(`/rules/${r.id}`, { method: 'PUT', body: { ...r, enabled } }); reload(); };
  const del = async (r) => { if (window.confirm(`Delete rule "${r.name}"?`)) { await api(`/rules/${r.id}`, { method: 'DELETE' }); reload(); } };
  const thr = (r) => [r.warn != null && `warn ${r.direction === 'below' ? '<' : '>'} ${fmtValue(r.warn, r.unit)}`, r.crit != null && `crit ${r.direction === 'below' ? '<' : '>'} ${fmtValue(r.crit, r.unit)}`].filter(Boolean).join(' · ');
  return (
    <div className="stack">
      <div className="row"><span className="muted">Thresholds are editable here; presets are only seeded once, so your edits survive restarts.</span>
        {can.edit && <button className="btn primary right" onClick={() => setEdit({})}>New rule</button>}</div>
      <ErrorNote>{error}</ErrorNote>
      <Card flush><div className="scroll"><table className="t">
        <thead><tr><th>On</th><th>Rule</th><th>Thresholds</th><th>For</th><th>State</th><th /></tr></thead>
        <tbody>{(data || []).map((r) => (
          <tr key={r.id}>
            <td>{can.edit ? <Toggle checked={r.enabled} onChange={(v) => toggle(r, v)} label={`Enable ${r.name}`} /> : <Badge>{r.enabled ? 'on' : 'off'}</Badge>}</td>
            <td><b>{r.name}</b> {r.datasource === 'loki' && <Badge>logs</Badge>} {r.servers?.length > 0 && <Badge kind="info">only: {r.servers.join(', ')}</Badge>}<div className="mono muted" style={{ maxWidth: 420, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.expr}>{r.expr}</div></td>
            <td>{thr(r)}</td>
            <td>{fmtDuration(r.for_seconds)}</td>
            <td>{r.error ? <Badge kind="warn" icon="alert">query error</Badge> : r.enabled ? <Badge kind="ok" icon="check">evaluating</Badge> : <Badge>disabled</Badge>}{r.error && <div className="muted" style={{ fontSize: 12 }}>{r.error.slice(0, 80)}</div>}</td>
            <td style={{ whiteSpace: 'nowrap' }}>{can.edit && <><button className="btn sm icon" onClick={() => setEdit(r)} aria-label={`Edit ${r.name}`}>Edit</button> <button className="btn sm danger" onClick={() => del(r)} aria-label={`Delete ${r.name}`}>Delete</button></>}</td>
          </tr>
        ))}</tbody>
      </table></div></Card>
      {edit && <RuleModal rule={edit.id ? edit : null} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); reload(); }} />}
    </div>
  );
}

function Silences() {
  const { can } = useApp();
  const { data, error, reload } = useApi('/silences', { interval: 20000 });
  const del = async (s) => { await api(`/silences/${s.id}`, { method: 'DELETE' }); reload(); };
  if (!data) return error ? <ErrorNote>{error}</ErrorNote> : null;
  if (!data.length) return <Card><Empty title="No active silences">Silence an alert from the Active tab during maintenance.</Empty></Card>;
  return (
    <Card flush><div className="list">{data.map((s) => (
      <div key={s.id} className="row">
        <Badge>silenced</Badge><b>{s.source_id || 'all rules'}</b>{s.server && <Badge kind="info">{s.server}</Badge>}{s.match && <span className="mono muted">matching “{s.match}”</span>}
        <span className="muted">until {fmtDateTime(s.until)}</span>{can.edit && <button className="btn sm right" onClick={() => del(s)}>Remove</button>}
      </div>))}</div></Card>
  );
}

export default function Alerts({ tab = 'active' }) {
  const tabs = [['active', 'Active'], ['history', 'History'], ['rules', 'Rules'], ['silences', 'Silences']];
  const { servers, multi } = useApp();
  const [server, setServer] = useState('');
  return (
    <div>
      <div className="tabs" role="tablist">{tabs.map(([id, label]) => (
        <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'on' : ''} onClick={() => { window.location.hash = `/alerts/${id}`; }}>{label}</button>
      ))}</div>
      {multi && ['active', 'history'].includes(tab) && (
        <div className="row" style={{ marginBottom: 12 }}>
          <label htmlFor="srv-filter" className="muted">Server</label>
          <select id="srv-filter" value={server} onChange={(e) => setServer(e.target.value)}>
            <option value="">All servers</option>
            {servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </div>
      )}
      {tab === 'active' && <Active server={server} />}
      {tab === 'history' && <History server={server} />}
      {tab === 'rules' && <Rules />}
      {tab === 'silences' && <Silences />}
    </div>
  );
}
