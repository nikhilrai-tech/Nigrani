import { useEffect, useState } from 'react';
import { api, useApi } from '../api';
import { Card, ErrorNote, Seg } from '../components/ui';
import { ExploreButton } from '../components/Grafana';
import { buildLogRule } from '../logrule';
import { RuleModal } from './Alerts';
import { useApp } from '../ctx';

const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
const levelOf = (line) => (/(error|exception|traceback|critical|fatal)/i.test(line) ? 'error' : /warn/i.test(line) ? 'warn' : '');

export default function Logs({ embedded = false, container: fixed = '' }) {
  const { range } = useApp();
  const containers = useApi('/containers', { enabled: !embedded });
  const [container, setContainer] = useState(fixed);
  const [level, setLevel] = useState('all');
  const [q, setQ] = useState('');
  const [term, setTerm] = useState('');
  const [live, setLive] = useState(embedded ? false : true);
  const [state, setState] = useState({ rows: [], error: null, loading: true, query: '', label: 'container' });
  const [creating, setCreating] = useState(false);

  useEffect(() => { const t = setTimeout(() => setTerm(q), 350); return () => clearTimeout(t); }, [q]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      const p = new URLSearchParams({ container, q: term, limit: embedded ? '60' : '300', since: embedded ? '1h' : range });
      if (level !== 'all') p.set('level', level);
      try {
        const r = await api(`/logs?${p}`);
        if (alive) setState({ rows: r.rows, error: null, loading: false, query: r.query, label: r.label });
      } catch (e) { if (alive) setState((s) => ({ ...s, error: e.message, loading: false })); }
    };
    load();
    if (!live) return () => { alive = false; };
    const t = setInterval(load, 5000);
    return () => { alive = false; clearInterval(t); };
  }, [container, level, term, live, range, embedded]);

  const body = (
    <>
      {!embedded && (
        <div className="row wrap" style={{ marginBottom: 12 }}>
          <select value={container} onChange={(e) => setContainer(e.target.value)} aria-label="Container">
            <option value="">All containers</option>
            {(containers.data || []).map((c) => <option key={c.name} value={c.name}>{c.service || c.name}</option>)}
          </select>
          <Seg value={level} options={['all', 'warn', 'error']} onChange={setLevel} />
          <input type="text" placeholder="Search text…" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 280 }} aria-label="Search logs" />
          <span style={{ marginLeft: 'auto' }}><ExploreButton expr={state.query} datasource="loki" label="Open in Grafana" /></span>
          <button className="btn sm" onClick={() => setCreating(true)} disabled={!state.query} title="Alert when this search matches too many lines">Create alert from this search</button>
          <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> Live (5s)</label>
        </div>
      )}
      <ErrorNote>{state.error}</ErrorNote>
      <div className="logs" style={embedded ? { maxHeight: 260 } : undefined}>
        {state.rows.length === 0 && !state.loading && <div className="muted">No log lines match.</div>}
        {state.rows.map((r, i) => (
          <div key={`${r.ts}-${i}`} className={`lv-${levelOf(r.line)}`}>
            <span className="ts">{clock(r.ts)}</span>
            {!container && !embedded && <span className="c">{r.container}</span>}
            {r.line}
          </div>
        ))}
      </div>
    </>
  );
  if (embedded) return body;
  return (
    <>
      <Card>{body}</Card>
      {creating && (
        <RuleModal
          initial={buildLogRule({ query: state.query, label: state.label, container, level, text: term })}
          onClose={() => setCreating(false)}
          onSaved={() => { setCreating(false); window.location.hash = '/alerts/rules'; }}
        />
      )}
    </>
  );
}
