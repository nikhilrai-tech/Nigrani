# Contributing

Thanks for helping. Nigrani is small on purpose: one container, one SQLite file, reads from Prometheus/Loki.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt pytest
(cd backend && python -m pytest -q)                  # backend tests

cd frontend && npm install && npm run dev            # UI with hot reload, proxies /api to :8080
```

See the README's **Development** section for running the backend against a local Prometheus.

## Presets

Presets are PromQL against third-party metric names. If you add or change one, verify it against the real exporter
with [dev/lab](dev/lab/README.md) and say which exporter version you used in the PR.

## Pull requests

- `main` is protected: open a PR; it must pass **backend, frontend, docker and gitleaks** and is merged by squash.
- Keep PRs focused, add or update tests, and update the docs and `nigrani.example.yaml` for any config change.
- UI changes: include a screenshot, and check both light and dark themes.
- **Never include secrets, tokens, internal hostnames or personal data** in code, tests, logs or screenshots.
  Use `${ENV_VAR}` references in examples. Enable the optional pre-commit hook to catch mistakes early:
  `pip install pre-commit && pre-commit install`.

## Reporting problems

Bugs and ideas: open an issue using the templates. Security problems: see [SECURITY.md](SECURITY.md).
