import { useApi } from '../api';
import { Badge, Card, Empty, ErrorNote, Icon } from '../components/ui';
import { fmtDateTime, fmtDuration } from '../format';

const TEXT = { operational: 'All systems operational', degraded: 'Partial degradation', major: 'Major outage' };

/** Public status page (enabled with status_page.enabled). No login, minimal data. */
export default function Status() {
  const { data, error } = useApi('/public/status', { interval: 30000 });
  if (error) return <div className="pub"><ErrorNote>{error}</ErrorNote></div>;
  if (!data) return <div className="pub muted">Loading…</div>;
  return (
    <div className="pub stack">
      <h1 style={{ margin: 0 }}>{data.title}</h1>
      <div className={`banner ${data.status}`}><Icon name={data.status === 'operational' ? 'check' : 'alert'} size={26} /><h2>{TEXT[data.status]}</h2></div>
      <Card title="Services" flush>
        {data.checks.length === 0 ? <Empty title="No public checks" /> : (
          <div className="list">{data.checks.map((c) => (
            <div key={c.name} className="row"><span className={`dot ${c.ok === false ? 'crit' : c.ok ? 'ok' : ''}`} /><b>{c.name}</b>
              <span className="muted right">{c.uptime_30d != null ? `${c.uptime_30d.toFixed(2)}% (30d)` : ''}</span>
              <Badge kind={c.ok === false ? 'crit' : 'ok'}>{c.ok === false ? 'Down' : 'Operational'}</Badge></div>))}</div>
        )}
      </Card>
      <Card title="Past 7 days" flush>
        {data.incidents.length === 0 ? <Empty title="No incidents" /> : (
          <div className="list">{data.incidents.map((i, n) => (
            <div key={n} className="row"><Badge kind={i.severity === 'critical' ? 'crit' : 'warn'}>{i.severity}</Badge><b>{i.rule}</b>
              <span className="muted right">{fmtDateTime(i.started_at)} · {i.resolved_at ? `resolved after ${fmtDuration(i.resolved_at - i.started_at)}` : 'ongoing'}</span></div>))}</div>
        )}
      </Card>
    </div>
  );
}
