import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fmtBytes, fmtDuration, fmtValue } from './format.js';

test('fmtValue formats ratio as percentage (0-1 fraction to 0-100%)', () => {
  assert.equal(fmtValue(0, 'ratio'), '0.0%');
  assert.equal(fmtValue(0.5, 'ratio'), '50.0%');
  assert.equal(fmtValue(0.8542, 'ratio'), '85.4%');
  assert.equal(fmtValue(1, 'ratio'), '100.0%');
  assert.equal(fmtValue(0.005, 'ratio'), '0.5%');
});

test('fmtValue handles edge cases for ratio and percent', () => {
  assert.equal(fmtValue(null, 'ratio'), '—');
  assert.equal(fmtValue(NaN, 'ratio'), '—');
  assert.equal(fmtValue(undefined, 'ratio'), '—');
  assert.equal(fmtValue(50, 'percent'), '50.0%');
  assert.equal(fmtValue(null, 'percent'), '—');
});

test('fmtValue handles bytes and duration units', () => {
  assert.equal(fmtValue(1024, 'bytes'), '1.0 KB');
  assert.equal(fmtValue(1048576, 'bytes'), '1.0 MB');
  assert.equal(fmtValue(0.05, 'seconds'), '50 ms');
  assert.equal(fmtValue(2.5, 'seconds'), '2.50 s');
  assert.equal(fmtBytes(null), '—');
  assert.equal(fmtDuration(125), '2m');
});
