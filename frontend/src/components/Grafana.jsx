import { useState } from 'react';
import { api, useApi } from '../api';
import { useApp } from '../ctx';
import { Icon } from './ui';

/** True when the selected server has a Grafana configured. */
export function useGrafana() {
  const { me, server } = useApp();
  return !!me.servers.find((s) => s.id === server)?.grafana;
}

const External = ({ href, children }) => (
  <a className="btn sm" href={href} target="_blank" rel="noopener noreferrer"><Icon name="external" size={13} />{children}</a>
);

/** Your own Grafana dashboards for this place in the UI (configured under grafana.dashboards). */
export function DashboardLinks({ context, container = '' }) {
  const on = useGrafana();
  const q = new URLSearchParams({ context, ...(container ? { container } : {}) });
  const { data } = useApi(`/grafana-links?${q}`, { enabled: on });
  if (!on || !data?.links?.length) return null;
  return <span className="row wrap">{data.links.map((l) => <External key={l.title} href={l.url}>{l.title}</External>)}</span>;
}

/** "Open in Grafana": Explore with the exact query. `url` if already known, otherwise built from `expr`. */
export function ExploreButton({ url, expr, datasource = 'prometheus', label = 'Open in Grafana' }) {
  const on = useGrafana();
  const [busy, setBusy] = useState(false);
  if (!on) return null;
  if (url) return <External href={url}>{label}</External>;
  const open = async () => {
    setBusy(true);
    try {
      const r = await api(`/grafana-links?${new URLSearchParams({ expr, datasource })}`);
      if (r.explore) window.open(r.explore, '_blank', 'noopener,noreferrer');
    } finally { setBusy(false); }
  };
  return <button className="btn sm" onClick={open} disabled={busy || !expr}><Icon name="external" size={13} />{label}</button>;
}
