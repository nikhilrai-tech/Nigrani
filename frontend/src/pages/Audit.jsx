import { useState } from 'react';
import { useApi } from '../api';
import { Badge, Card, Empty, ErrorNote, Seg } from '../components/ui';
import { fmtDateTime } from '../format';

const WINDOWS = { '24h': 1, '7d': 7, '30d': 30 };

export default function Audit() {
  const [win, setWin] = useState('7d');
  const [user, setUser] = useState('');
  const { data, error } = useApi(`/audit?days=${WINDOWS[win]}&limit=500${user ? `&user=${encodeURIComponent(user)}` : ''}`, { interval: 30000 });
  const users = [...new Set((data || []).map((a) => a.user))].sort();
  return (
    <div className="stack">
      <div className="row wrap">
        <span className="muted">Sign-ins and every change made through Nigrani, and who made it.</span>
        <span className="right row"><select value={user} onChange={(e) => setUser(e.target.value)} aria-label="Filter by user"><option value="">All users</option>{users.map((x) => <option key={x}>{x}</option>)}</select>
          <Seg value={win} options={Object.keys(WINDOWS)} onChange={setWin} /></span>
      </div>
      <ErrorNote>{error}</ErrorNote>
      <Card flush>
        {data && data.length === 0 ? <Empty title="Nothing recorded in this window" /> : (
          <div className="scroll"><table className="t">
            <thead><tr><th>When</th><th>User</th><th>Action</th><th>Detail</th><th>From</th></tr></thead>
            <tbody>{(data || []).map((a) => (
              <tr key={a.id}>
                <td style={{ whiteSpace: 'nowrap' }}>{fmtDateTime(a.ts)}</td>
                <td><b>{a.user}</b></td>
                <td>{a.action.startsWith('denied') ? <Badge kind="crit" icon="alert">{a.action}</Badge> : a.action.endsWith('failed') ? <Badge kind="warn" icon="alert">{a.action}</Badge> : <span className="mono">{a.action}</span>}</td>
                <td className="muted">{a.detail}</td>
                <td className="mono muted">{a.ip}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Card>
    </div>
  );
}
