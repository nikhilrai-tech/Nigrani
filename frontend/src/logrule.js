/**
 * Turns the search on the Logs page into a log-based alert rule (datasource: loki).
 *
 * `query` is the LogQL log selector the backend actually ran (selector + level/text filters), `label` is the
 * Loki label that names a container, so each container gets its own alert.
 */
export function buildLogRule({ query, label = 'container', container = '', level = 'all', text = '', windowMinutes = 5 }) {
  if (!query) throw new Error('no log query to build a rule from');
  const parts = [container || 'all containers'];
  if (level !== 'all') parts.push(`${level}s`);
  if (text) parts.push(`"${text}"`);
  return {
    name: `Logs: ${parts.join(' ')}`.slice(0, 80),
    datasource: 'loki',
    expr: `sum by (${label}) (count_over_time(${query} [${windowMinutes}m]))`,
    direction: 'above',
    warn: 10,
    crit: 50,
    for_seconds: 120,
    unit: 'short',
    summary: `Too many log lines matched this search in the last ${windowMinutes} minutes. Set the thresholds to your normal volume.`,
  };
}
