# Preset lab

Nigrani's dashboard pages and alert rules (`backend/nigrani/presets/*.yaml`) are PromQL written against the metric
names of third-party exporters. This lab runs the **real** services and exporters so those names can be checked
against reality instead of documentation.

```bash
cd dev/lab
docker compose up -d --build        # redis, postgres, mosquitto, django-prometheus app, celery + their exporters
./traffic.sh                        # ~1 min of load so rate() and histogram panels have data
python3 verify.py                   # prints every panel/rule with its live series count
docker compose down -v
```

Last verified (2026-11-01):

| Preset | Verified against |
|---|---|
| `django` | django-prometheus 2.5.0 (Django 6.0.9) |
| `redis` | redis_exporter v1.96.0, Redis 7.4 |
| `postgres` | postgres_exporter v0.20.1, PostgreSQL 16 |
| `mqtt` | sapcc/mosquitto-exporter (2021 build), Mosquitto 2.1 |
| `celery` | danihodovic/celery-exporter, Celery 5.6 |
| `core` | node-exporter + cAdvisor (see `docker-compose.full.yml` in the repo root) |

Not covered: `rds` (needs a real AWS account and yet-another-cloudwatch-exporter).

`verify.py` is expected to report `NO DATA` for the `core` rules here because the lab has no node-exporter or
cAdvisor. Anything else that is empty means an exporter renamed a metric and the preset needs updating.
