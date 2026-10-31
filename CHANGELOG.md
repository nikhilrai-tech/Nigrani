# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Custom message templates per notification channel (`title_template` / `body_template` with placeholders such as
  `{rule}`, `{series}`, `{value}`, `{server}`, `{duration}`), validated on save, with a live preview in the channel editor.
- Logs page: "Create alert from this search" turns the current container / level / text filter into a log-based alert
  rule, with the rule editor prefilled and a live preview.

## [0.1.0] - 2026-11-01

First public release.

### Added
- Dashboard: overview with threshold-aware tiles, containers, host (with disk-full forecast), logs (Loki),
  uptime checks (HTTP/TCP/TLS expiry), deploy markers from GitHub Actions, optional public status page.
- Presets for `core` (node-exporter + cAdvisor), `django`, `redis`, `postgres`, `rds`, `mqtt` and `celery`;
  custom pages and rules in YAML.
- Alert engine: pending/firing/resolved with de-duplication, reminders, escalation, silences, quiet hours,
  retry on delivery failure; rules editable in the UI with live preview.
- Notification channels: Slack, Microsoft Teams, Discord, Telegram, email, WhatsApp (Twilio), signed webhook.
- Incident history with a visual timeline, and a daily/weekly digest.
- Multiple servers (for example production and staging) in one dashboard, with an all-servers view and
  per-server rule/channel/silence scoping.
- Login, rate-limited, signed cookie sessions, CSRF protection and security headers.
- Log-based alert rules: `datasource: loki` evaluates a LogQL metric query against a server's Loki. New `logs` preset
  with an "Error spike in logs" rule; the rule editor can choose the data source and preview LogQL.
- `dev/lab`: real exporters used to verify the presets (django, redis, postgres, mqtt, celery).
- GitHub deploy markers ignore pull-request and Dependabot runs.
- Release automation: a `vX.Y.Z` tag builds and publishes a multi-arch image to `ghcr.io/nikhilrai-tech/nigrani`.

### Fixed
- A "resolved" message sent after a restart keeps the rule name and series, and a series that no longer exists
  no longer shows its last firing value as "Now".
