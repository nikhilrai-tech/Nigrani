import { useApi } from '../api';
import { Badge, Card, Empty, ErrorNote } from '../components/ui';
import { fmtAgo, fmtDuration } from '../format';

export default function Deploys() {
  const { data, error } = useApi('/deploys', { interval: 60000 });
  if (!data) return error ? <ErrorNote>{error}</ErrorNote> : <div className="muted">Loading…</div>;
  if (!data.enabled) return <Card><Empty title="GitHub not configured">Set <span className="mono">github.repo</span> (and optionally a token) in nigrani.yaml to see deploys here and as markers on charts.</Empty></Card>;
  return (
    <div className="stack">
      <ErrorNote>{data.error}</ErrorNote>
      <Card flush>
        <div className="list">
          {data.runs.map((r) => {
            const kind = r.status !== 'completed' ? 'info' : r.conclusion === 'success' ? 'ok' : 'crit';
            const label = r.status !== 'completed' ? r.status.replace('_', ' ') : r.conclusion;
            return (
              <div key={r.id} className="row wrap">
                <Badge kind={kind} icon={kind === 'ok' ? 'check' : kind === 'crit' ? 'alert' : undefined}>{label}</Badge>
                <div style={{ minWidth: 0 }}><b>{r.name}</b> <span className="muted">· {r.title}</span>
                  <div className="muted mono" style={{ fontSize: 12 }}>{r.branch}@{r.sha} · {r.actor}</div></div>
                <span className="muted right">{fmtAgo(r.started_at)}{r.finished_at && r.started_at ? ` · took ${fmtDuration(r.finished_at - r.started_at)}` : ''}</span>
                <a href={r.url} target="_blank" rel="noreferrer noopener">Open</a>
              </div>
            );
          })}
          {data.runs.length === 0 && <Empty title="No workflow runs found" />}
        </div>
      </Card>
    </div>
  );
}
