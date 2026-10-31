import asyncio

import httpx

from nigrani.config import GithubCfg
from nigrani.github import GithubDeploys


def runs_payload():
    def run(i, event, name="Deploy", branch="main", status="completed", conclusion="success"):
        return {"id": i, "name": name, "display_title": f"run {i}", "head_branch": branch, "head_sha": "abcdef1234567",
                "status": status, "conclusion": conclusion, "event": event, "actor": {"login": "dev"},
                "html_url": f"https://github.com/o/r/actions/runs/{i}", "created_at": "2026-01-01T10:00:00Z",
                "run_started_at": "2026-01-01T10:00:05Z", "updated_at": "2026-01-01T10:02:05Z"}
    return {"workflow_runs": [run(1, "push"), run(2, "dynamic", name="Dependabot Updates"), run(3, "pull_request"),
                              run(4, "workflow_dispatch", name="Other"), run(5, "push", status="in_progress", conclusion=None)]}


def make(cfg, handler):
    return GithubDeploys(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_parses_runs_and_skips_non_deploys():
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json=runs_payload())

    g = make(GithubCfg(repo="o/r", branch="main", token="tok", workflow="deploy"), handler)
    runs = asyncio.run(g.runs())
    assert [r["id"] for r in runs] == [1, 5]          # dynamic + pull_request dropped, workflow filter applied
    assert runs[0]["sha"] == "abcdef1" and runs[0]["finished_at"] - runs[0]["started_at"] == 120
    assert runs[1]["finished_at"] is None              # still running
    assert seen["params"]["branch"] == "main" and seen["auth"] == "Bearer tok"


def test_disabled_without_repo():
    g = make(GithubCfg(), lambda r: httpx.Response(500))
    assert not g.enabled and asyncio.run(g.runs()) == []


def test_errors_are_readable_and_last_good_result_is_kept():
    state = {"ok": True}

    def handler(req):
        return httpx.Response(200, json=runs_payload()) if state["ok"] else httpx.Response(404, json={})

    g = make(GithubCfg(repo="o/r"), handler)
    first = asyncio.run(g.runs())
    assert first and g.error is None
    state["ok"] = False
    g._cache = (0.0, g._cache[1])                       # expire the 60s cache
    assert asyncio.run(g.runs()) == first               # keeps showing the last good data
    assert "404" in g.error and "private" in g.error and "mozilla" not in g.error
