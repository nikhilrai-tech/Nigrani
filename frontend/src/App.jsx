import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, setCurrentServer, useApi, useHashRoute } from './api';
import { Icon, Seg } from './components/ui';
import { AppCtx, useApp } from './ctx';
import { lsGet, lsSet } from './format';
import Alerts from './pages/Alerts';
import Backup from './pages/Backup';
import Containers from './pages/Containers';
import Deploys from './pages/Deploys';
import Fleet from './pages/Fleet';
import Host from './pages/Host';
import Integrations from './pages/Integrations';
import Login from './pages/Login';
import Logs from './pages/Logs';
import Overview from './pages/Overview';
import PanelPage from './pages/PanelPage';
import Status from './pages/Status';
import Uptime from './pages/Uptime';

const RANGES = ['15m', '1h', '6h', '24h', '7d'];

function useTheme() {
  const media = typeof window !== 'undefined' ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  const [pref, setPref] = useState(() => lsGet('nigrani.theme', ''));
  const [sys, setSys] = useState(media ? media.matches : false);
  useEffect(() => {
    if (!media) return undefined;
    const on = (e) => setSys(e.matches);
    media.addEventListener('change', on);
    return () => media.removeEventListener('change', on);
  }, [media]);
  const theme = pref || (sys ? 'dark' : 'light');
  useEffect(() => {
    if (pref) document.documentElement.dataset.theme = pref; else delete document.documentElement.dataset.theme;
  }, [pref]);
  const toggle = () => { const n = theme === 'dark' ? 'light' : 'dark'; setPref(n); lsSet('nigrani.theme', n); };
  return [theme, toggle];
}

function Shell({ me, onLogout }) {
  const [range, setRangeState] = useState(() => (RANGES.includes(lsGet('nigrani.range', '')) ? lsGet('nigrani.range', '1h') : '1h'));
  const [theme, toggleTheme] = useTheme();
  const [server, setServerState] = useState(() => {
    const saved = lsGet('nigrani.server', '');
    return me.servers.some((s) => s.id === saved) ? saved : me.default_server;
  });
  setCurrentServer(server); // keep imperative api() calls pointed at the selected server
  const setServer = (id) => { setCurrentServer(id); setServerState(id); lsSet('nigrani.server', id); };
  const multi = me.servers.length > 1;
  const ctx = useMemo(() => ({ range, theme, me, server, servers: me.servers, multi, setServer }), [range, theme, me, server, multi]); // eslint-disable-line react-hooks/exhaustive-deps
  const setRange = (r) => { setRangeState(r); lsSet('nigrani.range', r); };
  return (
    <AppCtx.Provider value={ctx}>
      <Layout me={me} onLogout={onLogout} toggleTheme={toggleTheme} setRange={setRange} />
    </AppCtx.Provider>
  );
}

// Everything below the provider, so its useApi() calls follow the selected server.
function Layout({ me, onLogout, toggleTheme, setRange }) {
  const { range, theme, server, setServer, multi } = useApp();
  const route = useHashRoute();
  const [open, setOpen] = useState(false);
  const cur = me.servers.find((s) => s.id === server) || me.servers[0];
  const pages = useApi('/pages', { interval: 60000 });
  const firing = useApi('/incidents?status=firing', { interval: 20000 });

  useEffect(() => { setOpen(false); }, [route]);

  const [, section, sub] = route.split('/');
  const nav = useMemo(() => {
    const items = [
      ...(multi ? [{ to: '/fleet', label: 'All servers', icon: 'layers', key: 'fleet' }] : []),
      { to: '/overview', label: 'Overview', icon: 'grid', key: 'overview' },
      { to: '/containers', label: 'Containers', icon: 'box', key: 'containers' },
      { to: '/host', label: 'Host', icon: 'server', key: 'host' },
    ];
    const extra = (pages.data || []).map((p) => ({ to: `/p/${p.id}`, label: p.title, icon: p.icon, key: `p/${p.id}`, dim: !p.available }));
    const tail = [
      ...(cur.logs ? [{ to: '/logs', label: 'Logs', icon: 'file', key: 'logs' }] : []),
      { to: '/alerts/active', label: 'Alerts', icon: 'bell', key: 'alerts', count: firing.data?.length || 0 },
      { to: '/uptime', label: 'Uptime', icon: 'shield', key: 'uptime' },
      ...(cur.deploys ? [{ to: '/deploys', label: 'Deploys', icon: 'rocket', key: 'deploys' }] : []),
      { to: '/integrations', label: 'Integrations', icon: 'plug', key: 'integrations' },
      { to: '/backup', label: 'Backup', icon: 'download', key: 'backup' },
    ];
    return { items, extra, tail };
  }, [pages.data, firing.data, cur, multi]);

  const current = section === 'p' ? `p/${sub}` : section;
  const all = [...nav.items, ...nav.extra, ...nav.tail];
  const title = all.find((n) => n.key === current)?.label || 'Overview';
  useEffect(() => { document.title = `${title} · ${me.app}`; }, [title, me.app]);

  let view;
  switch (section) {
    case 'fleet': view = <Fleet />; break;
    case 'containers': view = <Containers />; break;
    case 'host': view = <Host />; break;
    case 'p': view = <PanelPage key={sub} id={sub} />; break;
    case 'logs': view = <Logs />; break;
    case 'alerts': view = <Alerts tab={sub || 'active'} />; break;
    case 'uptime': view = <Uptime />; break;
    case 'deploys': view = <Deploys />; break;
    case 'integrations': view = <Integrations />; break;
    case 'backup': view = <Backup />; break;
    default: view = <Overview />;
  }

  const link = (n) => (
    <a key={n.key} href={`#${n.to}`} className={`${current === n.key ? 'active' : ''} ${n.dim ? 'dim' : ''}`} aria-current={current === n.key ? 'page' : undefined}>
      <Icon name={n.icon} />{n.label}{n.count > 0 && <span className="count" aria-label={`${n.count} firing`}>{n.count}</span>}
    </a>
  );

  return (
    <>
      <div className="app">
        <aside className={`side ${open ? 'open' : ''}`}>
          <div className="brand"><Icon name="activity" size={22} />{me.app}</div>
          <nav className="nav" aria-label="Main">
            {nav.items.map(link)}
            {nav.extra.length > 0 && <div className="group">Services</div>}
            {nav.extra.map(link)}
            <div className="group">Operate</div>
            {nav.tail.map(link)}
          </nav>
          <div className="foot">Nigrani v{me.version}</div>
        </aside>
        <div className="main">
          <header className="top">
            <button className="btn icon menu-btn" onClick={() => setOpen(!open)} aria-label="Menu"><Icon name="menu" /></button>
            <h1>{title}</h1>
            {multi ? (
              <select className="server-pick" value={server} onChange={(e) => setServer(e.target.value)} aria-label="Server">
                {me.servers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            ) : <span className="badge info">{cur.environment}</span>}
            <span className="spacer" />
            {['overview', 'host', 'p', 'containers'].includes(section || 'overview') && <Seg value={range} options={RANGES} onChange={setRange} />}
            <button className="btn icon" onClick={toggleTheme} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}><Icon name={theme === 'dark' ? 'sun' : 'moon'} /></button>
            {me.features.auth && <button className="btn icon" onClick={onLogout} aria-label="Sign out"><Icon name="logout" /></button>}
          </header>
          <main className="content" key={server}>{view}</main>
        </div>
      </div>
    </>
  );
}

export default function App() {
  const route = useHashRoute();
  const [me, setMe] = useState(undefined); // undefined = loading, false = signed out

  const loadMe = useCallback(async () => {
    try { setMe(await api('/me')); } catch { setMe(false); }
  }, []);
  useEffect(() => {
    loadMe();
    const on = () => setMe(false);
    window.addEventListener('nigrani:unauth', on);
    return () => window.removeEventListener('nigrani:unauth', on);
  }, [loadMe]);

  if (route.startsWith('/status')) return <Status />;
  if (me === undefined) return <div className="login muted">Loading…</div>;
  if (me === false) return <Login onDone={loadMe} />;
  return <Shell me={me} onLogout={async () => { await api('/logout', { method: 'POST' }); setMe(false); }} />;
}
