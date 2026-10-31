import { Meter, Spark, SevBadge } from './ui';
import { fmtValue } from '../format';

/** A metric tile: value, trend, and the warn/crit thresholds drawn right on the bar. */
export default function Tile({ t }) {
  const max = t.unit === 'percent' ? 100 : Math.max(t.crit ?? t.warn ?? 1, t.value ?? 0) * 1.25 || 1;
  const hasThr = t.warn != null || t.crit != null;
  return (
    <div className={`card tile ${t.status === 'warn' ? 'warn' : t.status === 'crit' ? 'crit' : ''}`} title={t.description || ''}>
      <div className="t"><span>{t.title}</span><span className="right"><SevBadge sev={t.status} /></span></div>
      <div className="v">{t.error ? <span className="muted" style={{ fontSize: 15 }}>unavailable</span> : fmtValue(t.value, t.unit)}</div>
      <Spark points={t.spark} status={t.status} />
      {hasThr && t.value != null && <Meter value={t.value} max={max} warn={t.warn} crit={t.crit} status={t.status} />}
      {hasThr && (
        <div className="thr">
          {t.warn != null && <span>warn {t.direction === 'below' ? '<' : '>'} {fmtValue(t.warn, t.unit)}</span>}
          {t.crit != null && <span>crit {t.direction === 'below' ? '<' : '>'} {fmtValue(t.crit, t.unit)}</span>}
        </div>
      )}
    </div>
  );
}
