# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. You will get a response within a few days.

Useful details: the version or commit, how to reproduce, and what an attacker could do with it.

## Scope and expectations

Nigrani is meant to run on a private network or behind a reverse proxy with HTTPS. It holds notification
credentials (webhook URLs, bot tokens) and can reach your Prometheus/Loki, so treat it like any other
internal admin tool:

- Set a strong `NIGRANI_PASSWORD`, and set `NIGRANI_SECRET` so sessions survive restarts.
- Keep Prometheus, Loki and exporters off the public internet.
- Prefer `${ENV_VAR}` references for channel secrets so they never land in the database or in git.

## Never commit secrets

`.env` and your real `nigrani.yaml` are git-ignored. If you ever commit a credential by mistake, **rotate it
immediately**. Deleting the commit is not enough once it has been pushed.
