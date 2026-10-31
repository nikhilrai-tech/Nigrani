# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
- `dev/lab`: real exporters used to verify the presets.

### Known gaps
- The `rds` preset is not verified against a real AWS account.
- Delivery to Slack, Teams, Telegram, email, WhatsApp and Discord has been unit-tested but not against live services.
