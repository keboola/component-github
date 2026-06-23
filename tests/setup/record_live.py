"""Stage-2 LIVE recorder: re-record the publicly-recordable coverage cases against real GitHub.

Run with: ``uv run python tests/setup/record_live.py`` (needs a VALID PAT in ``secrets.json``).

For every endpoint that returns 200 against a small PUBLIC target (see ``LIVE_TARGETS``), this:

1. Injects the real PAT (read from ``secrets.json``) TRANSIENTLY into the in-memory client —
   the committed ``config.json`` keeps the ``DUMMY_PAT_NOT_REAL`` placeholder, never the token.
2. Runs the REAL component against ``api.github.com`` under plain ``vcrpy`` (record mode) with the
   component's ``VCR_SANITIZERS`` applied, so the recorded request ``Authorization`` is ``Bearer ***``.
3. Trims the recorded 2xx response to a few rows (``_trim_interactions``) so the committed cassette
   stays small and carries only a little public data.
4. Replays the trimmed cassette once to snapshot ``expected/data/out/`` — so expected output is
   exactly what the component emits from the committed (trimmed) cassette.

Endpoints that 403/404 with this token (no ``read:org``/``repo``; or needing membership/private
access) are NOT recorded here — they keep the synthetic cassettes from ``generate_cassettes.py``.
A real ``401`` case is (re-)recorded with a deliberately bad token. The rate-limit case stays the
synthetic 403 + ``x-ratelimit-remaining:0`` cassette (we will not exhaust a real 5000/hr budget).

``coverage_manifest.json`` is updated to mark each endpoint ``live`` or ``synthetic`` with the
observed status, so the report and any future re-record know the true split.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path
from runpy import run_path


@contextlib.contextmanager
def _suppress_sdk_vcr():
    """Disable ComponentBase auto-VCR while we drive vcrpy ourselves.

    ``ComponentBase._should_vcr_replay`` returns True when ``cassettes/requests.json`` exists, which
    makes the SDK wrap the action with its OWN VCR layer — conflicting with the vcrpy cassette this
    script manages. Force it False for the duration (mirrors what VCRTestDataDir does internally).
    """
    try:
        from keboola.component.base import ComponentBase
    except ImportError:
        yield
        return
    original = getattr(ComponentBase, "_should_vcr_replay", None)
    if original is not None:
        ComponentBase._should_vcr_replay = staticmethod(lambda: False)  # ty: ignore[invalid-assignment]
    try:
        yield
    finally:
        if original is not None:
            ComponentBase._should_vcr_replay = original


_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_FUNCTIONAL = _REPO / "tests" / "functional"
_COMPONENT = _REPO / "src" / "component.py"
sys.path.insert(0, str(_REPO / "src"))

FREEZE_TIME = "2026-06-23T12:00:00+00:00"
MAX_ROWS = 2  # trim recorded list/inner-array responses to at most this many rows (small cassettes)


def _secret_token() -> str:
    """Read the real PAT from secrets.json (never logged/committed)."""
    return json.loads((_REPO / "secrets.json").read_text())["parameters"]["#github_pat"]


# endpoint -> row params for the LIVE target (public + small). Only endpoints that returned 200
# in the probe are here; the 6 that 403/404 stay synthetic and are intentionally absent.
LIVE_TARGETS: dict[str, dict] = {
    # NOTE: `user` and `user_repos` are intentionally NOT here — they return the *token owner's*
    # own profile/repos (the maintainer's account: real name, email). Recording them would commit
    # personal data, so they stay synthetic (octocat shapes) even though they'd return 200.
    "organizations": {"organization": "github"},
    "organization_members": {"organization": "github"},
    "organization_repositories": {"organization": "github"},
    "organization_events": {"organization": "github"},
    "repository": {"repository": "octocat/Spoon-Knife"},
    "repository_branches": {"repository": "octocat/Spoon-Knife"},
    "repository_tags": {"repository": "octokit/octokit.rb"},
    "repository_commits": {"repository": "octocat/Spoon-Knife"},
    "repository_contributors": {"repository": "octocat/Spoon-Knife"},
    "repository_languages": {"repository": "octocat/Spoon-Knife"},
    "repository_topics": {"repository": "cli/cli"},  # Spoon-Knife has no topics; cli/cli does
    "repository_releases": {"repository": "cli/cli"},
    "repository_issues": {"repository": "cli/cli"},  # Spoon-Knife /issues returns only PRs
    "repository_issue_comments": {"repository": "octocat/Spoon-Knife"},
    "repository_pulls": {"repository": "octocat/Spoon-Knife"},
    "repository_pull_reviews": {"repository": "cli/cli", "pull_number": 13705},
    "repository_pull_review_comments": {"repository": "octocat/Spoon-Knife"},
    "repository_labels": {"repository": "octocat/Hello-World"},
    "repository_milestones": {"repository": "github/docs"},
    "repository_stargazers": {"repository": "octocat/Spoon-Knife"},
    "repository_subscribers": {"repository": "octocat/Spoon-Knife"},
    "repository_forks": {"repository": "octocat/Spoon-Knife"},
    "repository_workflows": {"repository": "cli/cli"},
    "repository_workflow_runs": {"repository": "cli/cli"},
    "repository_deployments": {"repository": "vercel/next.js"},
}

# Endpoints kept synthetic (with the observed reason) — used to update the manifest.
SYNTHETIC_REASON: dict[str, str] = {
    "user": "synthetic by choice (returns the token owner's own profile incl. name/email — PII)",
    "user_repos": "synthetic by choice (returns the token owner's own repositories — PII)",
    "user_orgs": "403 (token lacks read:org/user; /user/orgs forbidden)",
    "organization_teams": "403 (listing org teams needs org membership)",
    "team_members": "404 (team not visible without membership)",
    "organization_outside_collaborators": "403 (org admin only)",
    "repository_collaborators": "403 (needs push access)",
    "repository_webhooks": "404 (needs repo admin)",
}


def _coverage_dir(endpoint: str) -> Path:
    """Find the existing coverage_<NN>_<endpoint> dir created by generate_cassettes.py."""
    matches = sorted(_FUNCTIONAL.glob(f"coverage_*_{endpoint}"))
    # Guard against prefix collisions (e.g. repository vs repository_branches): require exact suffix.
    exact = [m for m in matches if m.name.split("_", 2)[2] == endpoint]
    if not exact:
        raise FileNotFoundError(f"no coverage dir for endpoint '{endpoint}'")
    return exact[0]


# Field names whose live values are not extracted by any endpoint schema but bloat the cassette
# or carry third-party PII. We redact emails (contributor PII in commits/workflow head_commit) and
# truncate big free-text fields (release/PR/issue bodies, descriptions) the component never reads.
_PII_KEYS = {"email"}
_BLOAT_TEXT_KEYS = {"body", "description", "message", "diff_hunk", "patch", "text"}
# Large sub-structures no endpoint schema extracts — dropped to keep committed cassettes small
# (e.g. release `assets` arrays, GraphQL `_links`, `reactions`, `permissions`).
_DROP_KEYS = {
    "assets",
    "_links",
    "reactions",
    "permissions",
    # workflow_runs carries large nested objects no schema reads:
    "pull_requests",
    "head_repository",
    "referenced_workflows",
}


def _scrub_record(obj):
    """Recursively redact PII emails, truncate large free-text, and drop unextracted bloat."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _DROP_KEYS:
                continue
            if k in _PII_KEYS and isinstance(v, str) and "@" in v:
                out[k] = "redacted@example.com"
            elif k in _BLOAT_TEXT_KEYS and isinstance(v, str) and len(v) > 80:
                out[k] = v[:80] + "…[truncated]"
            else:
                out[k] = _scrub_record(v)
        return out
    if isinstance(obj, list):
        return [_scrub_record(i) for i in obj]
    return obj


def _trim_interactions(cassette_path: Path) -> None:
    """Clean a live cassette: drop transient non-2xx interactions and trim 2xx bodies to MAX_ROWS.

    GitHub occasionally returns a flaky 5xx that the client transparently retries; both the 5xx and
    the successful retry land in the cassette. We keep only the 2xx interactions (a clean single
    success per endpoint — replay must not see a 5xx and sleep on backoff). Then top-level arrays /
    WRAPPED inner arrays are truncated to MAX_ROWS so the committed cassette stays small. Object /
    object-map payloads are left as-is. The pagination Link header is stripped (only page 1 recorded).
    """
    data = json.loads(cassette_path.read_text())
    kept = []
    for interaction in data.get("interactions", []):
        resp = interaction.get("response", {})
        code = resp.get("status", {}).get("code", 0)
        if code >= 300:
            continue  # drop transient 5xx / retried error interactions
        headers = resp.get("headers", {})
        for hk in [k for k in headers if k.lower() == "link"]:
            del headers[hk]
        body = resp.get("body", {})
        raw = body.get("string")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError, ValueError:
                parsed = None
            if parsed is not None:
                if isinstance(parsed, list) and len(parsed) > MAX_ROWS:
                    parsed = parsed[:MAX_ROWS]
                elif isinstance(parsed, dict):
                    for key, val in parsed.items():
                        if isinstance(val, list) and len(val) > MAX_ROWS:
                            parsed[key] = val[:MAX_ROWS]
                # Redact third-party PII (emails) and truncate unextracted free-text bloat.
                parsed = _scrub_record(parsed)
                body["string"] = json.dumps(parsed)
        kept.append(interaction)
    data["interactions"] = kept
    cassette_path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _sanitized_request(request):
    import component as component_module

    for s in component_module.VCR_SANITIZERS:
        request = s.before_record_request(request)
    return request


def _sanitized_response(response):
    import component as component_module

    for s in component_module.VCR_SANITIZERS:
        response = s.before_record_response(response)
    return response


def _record_live(endpoint: str, target: dict) -> tuple[str, int]:
    """Record one endpoint live against real GitHub; return (case_name, recorded status code)."""
    import vcr
    from keboola.vcr.recorder import JsonIndentedSerializer

    import component as component_module

    case_dir = _coverage_dir(endpoint)
    data_dir = case_dir / "source" / "data"
    cassette_dir = data_dir / "cassettes"
    cassette_path = cassette_dir / "requests.json"
    # Ensure the KBC datadir output tree exists for the recording run.
    (data_dir / "out" / "tables").mkdir(parents=True, exist_ok=True)
    (data_dir / "in" / "tables").mkdir(parents=True, exist_ok=True)

    # Point the committed config at the LIVE public target (keeps the placeholder token). The
    # config's target must match the recorded URI so replay (which rebuilds the path from config)
    # matches on path. The token field stays the DUMMY placeholder — never the real value.
    config_path = data_dir / "config.json"
    config = json.loads(config_path.read_text())
    config["parameters"].update(target)
    config["parameters"]["#github_token"] = "DUMMY_PAT_NOT_REAL"
    config_path.write_text(json.dumps(config, indent=2))

    # Committed config keeps the placeholder; inject the REAL token transiently into the client.
    token = _secret_token()
    orig_init = component_module.GitHubClient.__init__

    def patched_init(self, *a, **kw):  # use the real token, ignore the placeholder from config
        kw.pop("token", None)
        kw.setdefault("sleep", lambda _s: None)  # no real backoff sleep on a flaky 5xx during record
        orig_init(self, token, **kw)

    component_module.GitHubClient.__init__ = patched_init  # ty: ignore[invalid-assignment]

    # Bound the live recording to a SINGLE page: large public repos (cli/cli workflow_runs,
    # Spoon-Knife commits/issues/forks) would otherwise paginate to the end — thousands of live
    # requests and a huge cassette. Disabling Link-following caps it at one page; combined with the
    # response-row trim, the committed cassette stays tiny. (Pagination itself is covered by the
    # dedicated synthetic 08_run_pagination_two_pages case.)
    # NB: grab the raw descriptor from the class __dict__ so we can restore the *staticmethod*
    # (assigning back the unwrapped function would turn it into an instance method).
    orig_next_link_desc = component_module.GitHubClient.__dict__["_next_link"]
    component_module.GitHubClient._next_link = staticmethod(lambda _response: None)  # ty: ignore[invalid-assignment]

    my_vcr = vcr.VCR(
        serializer="json",
        cassette_library_dir=str(cassette_dir),
        record_mode="all",
        match_on=["method", "scheme", "host", "port", "path", "query"],
        before_record_request=_sanitized_request,
        before_record_response=_sanitized_response,
    )
    my_vcr.register_serializer("json", JsonIndentedSerializer())

    import os

    os.environ["KBC_DATADIR"] = str(data_dir)
    os.environ["KBC_DATA_TYPE_SUPPORT"] = "authoritative"
    cassette_path.unlink(missing_ok=True)
    try:
        with _suppress_sdk_vcr(), my_vcr.use_cassette("requests.json"):
            run_path(str(_COMPONENT), run_name="__main__")
    finally:
        component_module.GitHubClient.__init__ = orig_init  # ty: ignore[invalid-assignment]
        component_module.GitHubClient._next_link = orig_next_link_desc  # type: ignore[method-assign]

    _trim_interactions(cassette_path)  # drops transient 5xx + trims rows
    status = _first_status(cassette_path)  # the clean 2xx status
    _inject_metadata(cassette_path)
    _snapshot_expected(case_dir)
    return case_dir.name, status


def _first_status(cassette_path: Path) -> int:
    data = json.loads(cassette_path.read_text())
    for i in data.get("interactions", []):
        return int(i["response"]["status"]["code"])
    return 0


def _inject_metadata(cassette_path: Path) -> None:
    data = json.loads(cassette_path.read_text())
    data["_metadata"] = {"freeze_time": FREEZE_TIME, "recorded_at": FREEZE_TIME}
    cassette_path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _snapshot_expected(case_dir: Path) -> None:
    """Replay the trimmed cassette with plain vcrpy and snapshot out/ -> expected/.

    Replaying the TRIMMED cassette (not the recording run's output) makes the committed expected
    output exactly what the component emits from the committed cassette. We replay with plain vcrpy
    in record_mode='none' (under the cassette freeze time) rather than via VCRTestDataDir, to avoid
    the SDK's auto-VCR ``_should_vcr_replay`` hook that the tester monkeypatches.
    """
    import os

    import vcr
    from freezegun import freeze_time
    from keboola.vcr.recorder import JsonIndentedSerializer

    data_dir = case_dir / "source" / "data"
    cassette_dir = data_dir / "cassettes"
    out_dir = data_dir / "out"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "tables").mkdir(parents=True, exist_ok=True)

    my_vcr = vcr.VCR(
        serializer="json",
        cassette_library_dir=str(cassette_dir),
        record_mode="none",
        match_on=["method", "scheme", "host", "port", "path", "query"],
    )
    my_vcr.register_serializer("json", JsonIndentedSerializer())

    os.environ["KBC_DATADIR"] = str(data_dir)
    os.environ["KBC_DATA_TYPE_SUPPORT"] = "authoritative"
    with _suppress_sdk_vcr(), freeze_time(FREEZE_TIME), my_vcr.use_cassette("requests.json"):
        with contextlib.redirect_stdout(io.StringIO()):
            run_path(str(_COMPONENT), run_name="__main__")

    produced = out_dir / "tables"
    expected = case_dir / "expected" / "data" / "out"
    if expected.exists():
        shutil.rmtree(expected)
    (expected / "tables").mkdir(parents=True)
    if produced.exists() and any(produced.iterdir()):
        for f in produced.iterdir():
            shutil.copy2(f, expected / "tables" / f.name)
    shutil.rmtree(out_dir)


def _record_real_401() -> None:
    """Re-record the 21_run_unauthorized_fails case as a REAL 401 with a deliberately bad token."""
    import os

    import vcr
    from keboola.vcr.recorder import JsonIndentedSerializer

    import component as component_module

    case_dir = _FUNCTIONAL / "21_run_unauthorized_fails"
    data_dir = case_dir / "source" / "data"
    cassette_dir = data_dir / "cassettes"
    cassette_path = cassette_dir / "requests.json"

    orig_init = component_module.GitHubClient.__init__

    def patched_init(self, *a, **kw):  # deliberately bad token to elicit a real 401
        kw.pop("token", None)
        orig_init(self, "ghp_deliberatelyBadTokenForA401RecordingOnly00", **kw)

    component_module.GitHubClient.__init__ = patched_init  # ty: ignore[invalid-assignment]
    my_vcr = vcr.VCR(
        serializer="json",
        cassette_library_dir=str(cassette_dir),
        record_mode="all",
        match_on=["method", "scheme", "host", "port", "path", "query"],
        before_record_request=_sanitized_request,
        before_record_response=_sanitized_response,
    )
    my_vcr.register_serializer("json", JsonIndentedSerializer())
    os.environ["KBC_DATADIR"] = str(data_dir)
    cassette_path.unlink(missing_ok=True)
    try:
        with _suppress_sdk_vcr(), my_vcr.use_cassette("requests.json"):
            try:
                run_path(str(_COMPONENT), run_name="__main__")
            except SystemExit:
                pass
    finally:
        component_module.GitHubClient.__init__ = orig_init  # ty: ignore[invalid-assignment]
    _inject_metadata(cassette_path)
    (cassette_dir / "expected_status.json").write_text(json.dumps({"exit_code": 1}))
    print(f"  re-recorded REAL 401 -> 21_run_unauthorized_fails (status {_first_status(cassette_path)})")


def _update_manifest(live: dict[str, int]) -> None:
    manifest = {
        "scopes_observed": "read:packages",
        "live_recorded": {
            f"coverage_{_coverage_dir(ep).name.split('_', 2)[1]}_{ep}": f"{st}" for ep, st in sorted(live.items())
        },
        "synthetic_only": SYNTHETIC_REASON,
        "note": (
            "Live cassettes recorded against public GitHub data (small per-target row trim). "
            "Synthetic endpoints 403/404 with a read:packages-only PAT or need membership/"
            "private access; their shapes come from the documented GitHub schema."
        ),
    }
    (_HERE / "coverage_manifest.json").write_text(json.dumps(manifest, indent=2))


def main() -> None:
    live: dict[str, int] = {}
    for endpoint, target in LIVE_TARGETS.items():
        case_name, status = _record_live(endpoint, target)
        live[endpoint] = status
        print(f"  live {case_name:42s} status={status}")
    _record_real_401()
    _update_manifest(live)
    print(f"Done. Live-recorded {len(live)} endpoints; {len(SYNTHETIC_REASON)} kept synthetic.")


if __name__ == "__main__":
    main()
