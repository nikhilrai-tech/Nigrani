import { useState } from 'react';
import { api } from '../api';
import { ErrorNote, Icon } from '../components/ui';

export default function Login({ onDone }) {
  const [u, setU] = useState('admin');
  const [p, setP] = useState('');
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try { await api('/login', { method: 'POST', body: { username: u, password: p } }); onDone(); }
    catch (x) { setErr(x.message); }
    setBusy(false);
  };

  return (
    <div className="login">
      <form className="card" onSubmit={submit}>
        <div className="brand" style={{ padding: '0 0 12px' }}><Icon name="activity" size={24} />Nigrani</div>
        <label className="f" htmlFor="u">Username</label>
        <input id="u" type="text" value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" />
        <label className="f" htmlFor="p">Password</label>
        <input id="p" type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" autoFocus />
        <div style={{ marginTop: 12 }}><ErrorNote>{err}</ErrorNote></div>
        <button className="btn primary" style={{ width: '100%', justifyContent: 'center', marginTop: 16 }} disabled={busy || !p}>Sign in</button>
      </form>
    </div>
  );
}
