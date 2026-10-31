import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildLogRule } from './logrule.js';

test('builds a per-container count_over_time rule from the search the backend ran', () => {
  const r = buildLogRule({ query: '{container="api"} |~ "(?i)(error|exception)"', label: 'container', container: 'api', level: 'error' });
  assert.equal(r.datasource, 'loki');
  assert.equal(r.expr, 'sum by (container) (count_over_time({container="api"} |~ "(?i)(error|exception)" [5m]))');
  assert.equal(r.name, 'Logs: api errors');
  assert.ok(r.warn < r.crit);
});

test('names the rule after all containers and the search text', () => {
  const r = buildLogRule({ query: '{container=~".+"} |= "timeout"', label: 'service', text: 'timeout', windowMinutes: 10 });
  assert.equal(r.name, 'Logs: all containers "timeout"');
  assert.match(r.expr, /^sum by \(service\) \(count_over_time\(.*\[10m\]\)\)$/);
});

test('refuses to build a rule without a query', () => {
  assert.throws(() => buildLogRule({ query: '' }), /no log query/);
});

test('keeps very long search text out of the 80 character name limit', () => {
  const r = buildLogRule({ query: '{c="x"}', text: 'x'.repeat(200) });
  assert.ok(r.name.length <= 80);
});
