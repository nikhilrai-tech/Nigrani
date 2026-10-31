#!/usr/bin/env bash
# Generates load against every service in the lab so that rate()/histogram panels have data.
set -euo pipefail
cd "$(dirname "$0")"
C="docker compose -f compose.yml"
$C exec -T redis redis-cli config set maxmemory 10mb >/dev/null   # so the redis-memory rule has a limit to compare with
for round in 1 2 3; do
  $C exec -T web python - <<'PY' >/dev/null 2>&1 || true
import urllib.request, urllib.error
for path, n in (("/ok/", 30), ("/slow/", 6), ("/boom/", 6), ("/db/", 40)):
    for _ in range(n):
        try: urllib.request.urlopen("http://localhost:8000" + path, timeout=5).read()
        except urllib.error.HTTPError: pass
PY
  $C exec -T worker python -c "
from tasks import add, flaky
[add.delay(i, i) for i in range(20)]; [flaky.delay(i) for i in range(10)]" >/dev/null 2>&1 || true
  $C exec -T redis sh -c 'for i in $(seq 1 200); do redis-cli set k$i v$i >/dev/null; redis-cli get k$i >/dev/null; done' || true
  $C exec -T mosquitto sh -c 'for i in $(seq 1 30); do mosquitto_pub -h localhost -t lab/t -m "m$i"; done' || true
  $C exec -T postgres psql -U postgres -qc "create table if not exists t(i int); insert into t select generate_series(1,500); select count(*) from t;" >/dev/null 2>&1 || true
  sleep 20
done
echo "traffic done; give Prometheus ~30s, then run: python3 verify.py"
