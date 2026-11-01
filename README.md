# Nigrani

*Nigrani* (निगरानी) is Hindi for "watchful oversight".

A self-hosted monitoring and alerting dashboard you can drop into any Docker project.

Nigrani sits **on top of** Prometheus (and optionally Loki). It doesn't replace them. It gives you one place to see
what's healthy, what the thresholds are, what is about to break, and it sends you a message on Slack, Microsoft
Teams, Telegram, email, WhatsApp, Discord or any webhook when something does.

![Overview](docs/screenshots/overview-dark.png)

## Why

Grafana is great for exploring metrics but it won't tell you "the disk fills in 1.4 days" or page you when a container
silently stops. Nigrani answers the operational questions directly:

- **Is everything OK right now?** One status banner. Every tile shows its warn/critical thresholds on the bar.
- **What is about to break?** Disk-full forecast, crash-loop detection, scrape targets that went down.
- **Tell me when it does.** An alert engine with `pending → firing → resolved`, de-duplication, reminders, silences,
  quiet hours, and automatic retry if a channel is down.
- **Why?** Per-container CPU/memory/network, live logs (Loki), and deploy markers on the charts so you can see
  that the spike started right after a release.

## Features

| | |
|---|---|
| **Overview** | Status banner, key tiles with thresholds, CPU/memory trend, active alerts, uptime checks |
| **Containers** | Every container: status, CPU, memory (vs limit), restarts, uptime, network. Click for charts and logs. Auto-detects stopped containers |
| **Host** | CPU, memory, load, network, disk I/O, per-filesystem usage with **days-until-full forecast** |
| **Grafana links** | "Open in Grafana" from an alert, a rule or the Logs page (Explore with the same query and time range), plus links to your own dashboards from the incident, container, host and overview screens |
| **Service pages** | Traffic (Django), Redis, Postgres, AWS RDS, MQTT, Celery out of the box. Add your own pages in YAML |
| **Incident page** | Click any alert for everything around it: lifecycle timeline, the metric with thresholds, the container's CPU/memory and logs, deploys and restarts just before it started, and other alerts that fired at the same time ("What changed?") |
| **Alerts** | Rules editable in the UI (thresholds, duration, PromQL with live preview), silences, and an **incident timeline** showing when each alert started, how long it lasted and what its peak was |
| **Integrations** | Slack, Teams, Discord, Telegram, Email, WhatsApp (Twilio), signed webhook. Per-channel severity + quiet hours, "send test" button |
| **Uptime** | HTTP / TCP / TLS-certificate-expiry checks with 30-day uptime and an optional public status page |
| **Deploys** | GitHub Actions runs (pull-request and Dependabot runs are ignored), shown as dashed markers on charts |
| **Digest** | A daily or weekly summary message |
| **Backup** | Export and restore rules, channels and silences as one JSON file, with a dry-run before anything changes |
| **Multiple servers** | Watch production and staging (or any number of environments) from one dashboard, with an all-servers view and per-server routing of alerts |
| **UI** | Light/dark, responsive, keyboard accessible, no external fonts or CDNs |

<p>
  <img src="docs/screenshots/containers.png" width="49%" alt="Containers" />
  <img src="docs/screenshots/host.png" width="49%" alt="Host" />
</p>
<p>
  <img src="docs/screenshots/alert-rules.png" width="49%" alt="Alert rules" />
  <img src="docs/screenshots/overview-light.png" width="49%" alt="Light theme" />
</p>
<p>
  <img src="docs/screenshots/fleet.png" width="49%" alt="All servers" />
  <img src="docs/screenshots/incident-timeline.png" width="49%" alt="Incident timeline" />
</p>

## Quick start

### A. Try it in two minutes (everything included)

Starts Prometheus, node-exporter, cAdvisor, Loki, Promtail and Nigrani, and monitors the machine it runs on.

```bash
git clone https://github.com/nikhilrai-tech/Nigrani.git && cd Nigrani
cp .env.example .env            # set NIGRANI_PASSWORD
docker compose -f docker-compose.full.yml up -d --build
# open http://localhost:8080  (user: admin)
```

Give it about a minute for Prometheus to collect its first samples.

### B. Add Nigrani to the stack you already have

1. Copy [nigrani.example.yaml](nigrani.example.yaml) to `nigrani.yaml`; point `datasources.prometheus` at your Prometheus
   and pick the `presets` that match the exporters you run.
2. Add the service from [examples/add-to-existing-compose.yml](examples/add-to-existing-compose.yml) to your
   `docker-compose.yml` (or run [docker-compose.yml](docker-compose.yml) from this repo, joining your network).
3. Set `NIGRANI_PASSWORD` in your `.env` and `docker compose up -d`.

### C. Plain `docker run`

Once a release is published you can use `ghcr.io/nikhilrai-tech/nigrani:latest` instead of building.

```bash
docker build -t nigrani .
docker run -d --name nigrani -p 8080:8080 \
  -e NIGRANI_PASSWORD=... -e NIGRANI_PROMETHEUS_URL=http://your-prometheus:9090 \
  -v nigrani-data:/data nigrani
```

Without a config file Nigrani runs with the `core` preset and these environment variables:
`NIGRANI_PASSWORD` (required), `NIGRANI_USERNAME`, `NIGRANI_PROMETHEUS_URL`, `NIGRANI_LOKI_URL`, `NIGRANI_SECRET`
(session-signing key; set it so logins survive restarts), `NIGRANI_DATA_DIR`, `NIGRANI_CONFIG`.

## Monitoring several servers (prod + staging)

Add a `servers:` list. Each server has its own Prometheus and, optionally, its own Loki, `services`, uptime `checks`
and GitHub repo; anything you leave out is inherited from the top level. See
[examples/multi-server.yaml](examples/multi-server.yaml).

```yaml
servers:
  - { id: prod,    name: Production, datasources: { prometheus: "http://10.0.1.20:9090" } }
  - { id: staging, name: Staging,    datasources: { prometheus: "http://10.0.2.20:9090" } }
channels:
  - { id: oncall, name: On-call, type: teams, settings: { webhook_url: "${TEAMS_WEBHOOK_URL}" }, servers: [prod] }
```

- A **server switcher** in the header and an **All servers** screen showing each environment's health at a glance.
- Every rule is evaluated against every server's Prometheus; each server alerts **independently** (the same disk
  alert can be firing on prod and resolved on staging). Notifications are tagged `[Production]` / `[Staging]`.
- Scope a rule with `servers: [staging]` (for example a looser threshold) and a channel with `servers: [prod]`
  (so staging noise never pages on-call). Both can also be set from the UI.
- Silences can target one server. Alert history and the timeline show all servers, filterable by server.
- Nigrani only needs to *reach* each Prometheus: same network, VPN, private IP or an SSH tunnel. Keep them off the
  public internet.
- Without `servers:` everything works as a single implicit server, and existing databases upgrade in place.
  (If you add `servers:` to an existing setup, open incidents from the old implicit server are closed on startup.)

## What Nigrani needs from your stack

Nigrani only reads from Prometheus/Loki. Which pages light up depends on which exporters are being scraped:

| Preset | Needs | You get |
|---|---|---|
| `core` (default) | [node-exporter](https://github.com/prometheus/node_exporter), [cAdvisor](https://github.com/google/cadvisor) | Host, Containers, host/disk/restart alerts |
| `django` | [django-prometheus](https://github.com/korfuri/django-prometheus) | Traffic page, 5xx + latency alerts |
| `redis` | [redis_exporter](https://github.com/oliver006/redis_exporter) | Redis page, memory alerts |
| `postgres` | [postgres_exporter](https://github.com/prometheus-community/postgres_exporter) | Postgres page, connection alerts |
| `rds` | [yet-another-cloudwatch-exporter](https://github.com/prometheus-community/yet-another-cloudwatch-exporter) | AWS RDS page, CPU/storage alerts |
| `mqtt` | a Mosquitto exporter | MQTT page |
| `celery` | [celery-exporter](https://github.com/danihodovic/celery-exporter) | Celery page, queue/worker alerts |
| `logs` | Loki (`datasources.loki`) | A log-based alert: error spike per container |

A page whose metrics are missing shows which exporter to add, instead of empty charts.

The `django`, `redis`, `postgres`, `mqtt` and `celery` presets are checked against the real exporters in
[dev/lab](dev/lab) (versions listed there); `core` is checked against node-exporter + cAdvisor.
Logs need Loki with a container-name label (see [deploy/promtail.yml](deploy/promtail.yml)).

> **Docker 29+ and cAdvisor:** cAdvisor older than v0.53 can't talk to the Docker 29 API, so container names are
> missing and the Containers page stays empty. Use `ghcr.io/google/cadvisor:v0.53.0` or newer.

## Configuration

Everything is in one YAML file; see the commented [nigrani.example.yaml](nigrani.example.yaml). Highlights:

```yaml
presets: [core, django, redis, celery]

checks:
  - { id: api, name: API, type: http, target: "https://api.example.com/health/" }

channels:
  - id: slack-ops
    name: "Slack #ops"
    type: slack
    settings: { webhook_url: "${SLACK_WEBHOOK_URL}" }   # read from the environment when sending
    min_severity: warning

pages:                       # your own dashboard page
  - id: devices
    title: Devices
    requires: devices_online
    panels:
      - { title: Devices online, kind: area, expr: "sum(devices_online)", legend: online }
```

- Unknown keys are rejected at startup, so typos fail loudly.
- `${VAR}` and `${VAR:-default}` are expanded from the environment. Channel secrets are expanded at **send time**
  and never written to the database.
- Rules, channels and silences created in the UI live in SQLite (`/data/nigrani.db`). Preset rules are seeded once,
  so edits made in the UI survive restarts and upgrades.

### Alert rules

A rule is a PromQL expression plus thresholds. Every returned series is evaluated on its own (one alert per
container, per mount point, ...).

```yaml
rules:
  - id: queue-depth
    name: Job queue backing up
    expr: sum(celery_queue_length)
    warn: 100
    crit: 500
    for_seconds: 300      # must stay breached for 5 minutes before alerting
    direction: above      # or "below"
```

**Log-based rules.** A rule can read from Loki instead of Prometheus (`datasource: loki`). The expression is a LogQL
*metric* query, so it returns numbers and goes through the same thresholds, de-duplication, silences and channels:

```yaml
rules:
  - id: payment-errors
    name: Payment errors
    datasource: loki
    expr: sum(count_over_time({container="api"} |= "PaymentError" [5m]))
    crit: 10
    for_seconds: 0
```

On the Logs page, *Create alert from this search* builds one of these rules from the container, level and text you
are filtering on.

Servers without Loki skip log rules. The rule editor offers the data source when any server has Loki.
For a log rule, a series that disappears means "no matching lines", so the alert resolves on the next cycle instead of
after `alerts.absent_grace`. If Loki itself is down the rule shows a query error and nothing is resolved.

Behaviour worth knowing:

- **Pending → firing:** nothing is sent until the condition holds for `for_seconds`.
- **De-duplicated:** one message when it starts, one when it's resolved. Reminders every `alerts.repeat_interval`.
- **Preset rules stay current.** Preset rules are written to the database once so you can tune them. A fix shipped in a
  later release reaches rules you have **not** edited (it is applied on startup and logged); a rule whose expression or
  thresholds you changed is never overwritten.
- **Escalation:** warning → critical sends a new message.
- **Acknowledge:** on the Alerts page, *Acknowledge* (with an optional note, for 1h / 4h / 24h or until it resolves)
  tells your channels "✋ X is on it" once and pauses that alert's reminders. If it gets worse (warning to critical)
  or the time is up you are notified again; the resolved message credits who acknowledged it.
- **Delivery failures are retried** on the following evaluation cycles; failures are shown on the channel.
- **Restart-safe:** open incidents are reloaded, so a restart doesn't re-page you.
- **Silences** suppress messages but the incident is still recorded.
- **Meta alert:** if Nigrani itself can't reach Prometheus for 3 cycles you get a critical alert.
- **Containers:** configured `services` (and any container that ran for 10+ minutes) raise *Container down* when
  they stop. Stop tracking one from its drawer in the UI.

### Notification channels

| Type | Settings |
|---|---|
| `slack` | `webhook_url` (incoming webhook) |
| `teams` | `webhook_url` (a Teams *Workflows* webhook; Adaptive Card) |
| `discord` | `webhook_url` |
| `telegram` | `bot_token`, `chat_id` |
| `email` | `host`, `port`, `username`, `password`, `from`, `to` |
| `whatsapp` | `account_sid`, `auth_token`, `from_number`, `to_number` (via Twilio) |
| `webhook` | `url`, optional `secret` (adds an `X-Nigrani-Signature: sha256=<hmac>` header) |

Each channel has a minimum severity and optional quiet hours (critical alerts always come through).

**Custom messages.** Each channel can have its own title and body template, for example a runbook link in Slack and a
short line in Telegram. Leave them empty for the default text. The editor shows a live preview on a sample alert and
rejects unknown placeholders.

```yaml
channels:
  - id: slack-ops
    name: "Slack #ops"
    type: slack
    settings: { webhook_url: "${SLACK_WEBHOOK_URL}" }
    title_template: "{emoji} {rule} on {server}: {value}"
    body_template: "{series} for {duration}. {summary} Runbook: https://wiki.example.com/{rule}"
```

Placeholders: `{emoji}` `{status}` `{severity}` `{rule}` `{series}` `{value}` `{threshold}` `{server}` `{environment}`
`{duration}` `{summary}` `{link}` `{started_at}` `{kind}` `{acknowledged_by}` `{note}`, plus `{title}` and `{body}` for the default text (so you can
extend it instead of replacing it). Digests are not templated.

## Grafana links

Already have Grafana dashboards? Nigrani can link to them. It never talks to Grafana; it only builds URLs for your browser.

```yaml
grafana:
  url: https://grafana.example.com           # the address your browser uses
  prometheus_datasource: <datasource UID>    # Connections > Data sources > the UID in the URL
  loki_datasource: <datasource UID>
  dashboards:
    - title: Container dashboard
      url: /d/abc123/containers?var-container={container}&from={from}&to={to}
      where: [incident, container]           # incident | container | host | overview
    - { title: Host, url: /d/xyz789/node-exporter, where: [host, overview] }
```

- **Open in Grafana** (incident page, rule editor, Logs page) opens Explore (Grafana 10+) with the exact query and,
  for an incident, the time range around it.
- **Dashboard links** appear where you list them. Placeholders `{container}` `{server}` `{rule}` `{series}` `{from}`
  `{to}` (milliseconds) are URL-encoded; a link that needs a value the place doesn't have (a container on the host
  page) is left out. Relative URLs are joined to `grafana.url`.
- With `servers:` each server can have its own `grafana:` block.

## Backup and restore

The **Backup** page (or `GET /api/backup`) exports everything you configured in the UI (alert rules, notification
channels and active silences) as one JSON file; use it to move to a new server or recover from a mistake. Incident
history is not included.

- **Credentials are left out by default.** Webhook URLs and tokens are exported as `null`; values that reference an
  environment variable (`${SLACK_URL}`) are kept because they are not secrets. *Include credentials* produces a file
  with working secrets: treat it like a password and never commit it.
- **Restore shows a dry run first.** Choosing a file lists what would be created, overwritten or skipped. If anything
  in the file is invalid (an unknown server, inverted thresholds, a typo in a message template...) nothing is imported.
- **Nothing is ever deleted.** *merge* keeps existing rules and channels that have the same id; *overwrite* replaces them.
  A credential missing from the file never erases the one already stored.
- A channel whose credentials were not in the file is created **disabled** and listed, so it cannot fail silently.

The API equivalent is `POST /api/backup/import?mode=merge|overwrite&dry_run=true|false` (dry run is the default).

## Security

- The UI and API require a login. Nigrani refuses to start without a password
  (`NIGRANI_ALLOW_NO_AUTH=1` exists for local development only).
- Login is rate-limited, sessions are signed `HttpOnly` cookies, and state-changing requests need a custom header
  (CSRF protection). Secrets are masked in API responses.
- **Serve it over HTTPS** (a reverse proxy such as Caddy/Traefik/nginx, or a VPN) when exposed to the internet.
- **Keep Prometheus, Loki and the exporters private.** Nigrani is the only thing that needs to be reachable. In
  particular don't publish Prometheus' port to the internet; it has no authentication.
- Never commit `.env` or your real `nigrani.yaml` (both are in `.gitignore`).
- Run **one** Nigrani per database (`/data`). Two instances sharing a SQLite file would each raise their own copy of every alert.

## Architecture

```
Prometheus ─┐
Loki ───────┼─►  Nigrani backend (FastAPI)  ──►  Slack / Teams / Telegram / email / webhook ...
GitHub API ─┤    rules · incidents · checks
HTTP/TCP ───┘    SQLite (/data)
                       ▲
                       └──  React UI (served by the same container)
```

One container, one SQLite file, no extra services. Backend in [backend/nigrani](backend/nigrani), UI in
[frontend/src](frontend/src), built-in dashboards in [backend/nigrani/presets](backend/nigrani/presets).

## Development

```bash
# backend
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt pytest
(cd backend && python -m pytest -q)
NIGRANI_ALLOW_NO_AUTH=1 NIGRANI_PROMETHEUS_URL=http://localhost:9090 NIGRANI_DATA_DIR=./data \
  uvicorn nigrani.main:app --app-dir backend --port 8080

# frontend (proxies /api to :8080)
cd frontend && npm install && npm run dev
```

Presets are PromQL against third-party metric names, so they are tested against real exporters: see
[dev/lab/README.md](dev/lab/README.md) (`docker compose up`, `./traffic.sh`, `python3 verify.py`).

To add a preset, drop a YAML file with `tiles`, `pages` and `rules` into `backend/nigrani/presets/` (see
`redis.yaml` for the shape); it is picked up by name.

## License

MIT. See [LICENSE](LICENSE).
