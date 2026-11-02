import { useEffect, useMemo, useRef } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { fmtClock, fmtDateTime, fmtValue } from '../format';

const VARS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'];

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
}

/** Merge series with differing timestamps onto a shared x axis. */
export function align(series) {
  const set = new Set();
  series.forEach((s) => s.points.forEach((p) => set.add(p[0])));
  const xs = [...set].sort((a, b) => a - b);
  const idx = new Map(xs.map((x, i) => [x, i]));
  const ys = series.map((s) => {
    const a = new Array(xs.length).fill(null);
    s.points.forEach(([t, v]) => { a[idx.get(t)] = v; });
    return a;
  });
  return [xs, ...ys];
}

const NO_MARKERS = [];
const NO_THRESHOLDS = [];

export default function Chart({ series, unit = 'short', kind = 'line', height = 190, markers = NO_MARKERS, thresholds = NO_THRESHOLDS, theme, legend = true }) {
  const host = useRef(null);
  const tip = useRef(null);
  const plot = useRef(null);
  const data = useMemo(() => align(series), [series]);
  const hasData = series.length > 0 && data[0].length > 0;

  useEffect(() => {
    if (!hasData || !host.current) return undefined;
    const el = host.current;
    const colors = series.map((_, i) => cssVar(VARS[i % VARS.length]));
    const grid = cssVar('--border'); const text = cssVar('--muted');
    const axis = (extra) => ({ stroke: text, grid: { stroke: grid, width: 1 }, ticks: { show: false }, font: '11px system-ui', ...extra });
    const opts = {
      width: el.clientWidth, height, padding: [8, 22, 0, 0],
      cursor: { points: { size: 6 }, drag: { x: false, y: false } },
      legend: { show: false },
      scales: {
        x: { time: true },
        y: { range: (_u, min, max) => { const top = Math.max(max, ...thresholds.map((t) => t.value)); return [Math.min(0, min), unit === 'percent' ? Math.max(100, top) : unit === 'ratio' ? Math.max(1, top) : (top > 0 ? top * 1.15 : 1)]; } },
      },
      axes: [
        axis({ values: (_u, v) => v.map((t) => fmtClock(t)) }),
        axis({ size: 62, values: (_u, v) => v.map((x) => fmtValue(x, unit)) }),
      ],
      series: [{}, ...series.map((s, i) => ({
        label: s.name, stroke: colors[i], width: 1.6, points: { show: false }, spanGaps: false,
        fill: kind === 'area' ? `${colors[i]}22` : undefined,
      }))],
      hooks: {
        draw: [(u) => {
          const { ctx, bbox } = u;
          thresholds.forEach((t) => {
            const y = Math.round(u.valToPos(t.value, 'y', true));
            if (y < bbox.top || y > bbox.top + bbox.height) return;
            ctx.save();
            ctx.strokeStyle = t.color; ctx.fillStyle = t.color; ctx.globalAlpha = 0.85; ctx.setLineDash([2, 4]); ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.moveTo(bbox.left, y); ctx.lineTo(bbox.left + bbox.width, y); ctx.stroke();
            ctx.setLineDash([]); ctx.font = `${11 * devicePixelRatio}px system-ui`; ctx.textAlign = 'right';
            ctx.fillText(t.label, bbox.left + bbox.width - 4, y - 5 * devicePixelRatio);
            ctx.restore();
          });
          markers.forEach((m) => {
            const x = Math.round(u.valToPos(m.ts, 'x', true));
            if (x < bbox.left || x > bbox.left + bbox.width) return;
            ctx.save();
            ctx.strokeStyle = m.color || cssVar('--accent'); ctx.globalAlpha = 0.8;
            ctx.setLineDash([5, 4]); ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.moveTo(x, bbox.top); ctx.lineTo(x, bbox.top + bbox.height); ctx.stroke();
            ctx.restore();
          });
        }],
        setCursor: [(u) => {
          const t = tip.current; if (!t) return;
          const i = u.cursor.idx;
          if (i == null || u.cursor.left < 0) { t.style.display = 'none'; return; }
          const rows = series.map((s, k) => `<div class="r"><i style="background:${colors[k]}"></i>${s.name}: <b>${fmtValue(u.data[k + 1][i], unit)}</b></div>`).join('');
          const near = markers.find((m) => Math.abs(m.ts - u.data[0][i]) < (u.data[0][1] - u.data[0][0]) * 1.5 || 0);
          t.innerHTML = `<div class="tt">${fmtDateTime(u.data[0][i])}</div>${rows}${near ? `<div class="tt">▸ ${near.label}</div>` : ''}`;
          t.style.display = 'block';
          const left = u.cursor.left + 14; const w = t.offsetWidth;
          t.style.left = `${left + w > el.clientWidth ? u.cursor.left - w - 14 : left}px`;
          t.style.top = `${Math.max(0, u.cursor.top - 10)}px`;
        }],
      },
    };
    const u = new uPlot(opts, data, el);
    plot.current = u;
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height }));
    ro.observe(el);
    return () => { ro.disconnect(); u.destroy(); plot.current = null; };
  }, [data, series, unit, kind, height, markers, thresholds, theme, hasData]);

  if (!hasData) {
    return <div className="empty" style={{ padding: '28px 0' }}>No data in this range.</div>;
  }
  const last = (s) => { for (let i = s.points.length - 1; i >= 0; i--) if (s.points[i][1] != null) return s.points[i][1]; return null; };
  return (
    <div>
      <div className="chart" style={{ position: 'relative' }}>
        <div ref={host} />
        <div ref={tip} className="tip" />
      </div>
      {legend && series.length > 0 && (
        <div className="legend">
          {series.map((s, i) => (
            <span key={s.name + i}><i style={{ background: `var(${VARS[i % VARS.length]})` }} />{s.name}<b>{fmtValue(last(s), unit)}</b></span>
          ))}
        </div>
      )}
    </div>
  );
}
