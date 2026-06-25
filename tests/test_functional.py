"""VCR/datadir functional tests — replay committed cassettes, no network, no credentials.

Each case under ``tests/functional/<NN_name>/`` is a standard ``keboola.datadirtest`` test:

* ``source/data/config.json``          — the MERGED (root+row flattened) platform config, with
  placeholder secrets only (``#github_token`` / OAuth ``#data`` are dummies).
* ``source/data/cassettes/requests.json`` — the recorded HTTP interactions (vcrpy JSON). The
  request ``Authorization`` header reads ``Bearer ***`` (sanitized); no real token anywhere.
* ``source/data/in/state.json``        — optional seeded incremental watermark.
* ``source/data/cassettes/expected_status.json`` — for failure cases, the expected non-zero exit.
* ``expected/data/out/tables/*``       — for SUCCESS run cases, the exact CSV + manifest produced.

The component is run for real against the replayed cassette; output is diffed against ``expected/``,
the captured exit code is checked against ``expected_status.json``, and (for sync actions) the
returned value is compared against ``sync_action_result.json``. Time is frozen from the cassette
``_metadata.freeze_time`` so incremental ``last_run`` is deterministic.

Cassettes are AUTHORED for the synthetic / failure / pagination / envelope-shape cases (Stage 1)
and RECORDED live for the breadth success set (Stage 2). Either way replay is offline and
deterministic.
"""

import json
import os
from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRTestDataDir, get_test_cases

_HERE = Path(__file__).resolve().parent
_FUNCTIONAL_DIR = _HERE / "functional"
_COMPONENT_SCRIPT = _HERE.parent / "src" / "component.py"

# Native-type schema manifest is only emitted when the platform signals authoritative type
# support; set it so the recorded/expected manifests carry real base types (matches production
# when the Dev Portal dataTypeSupport property is enabled).
os.environ.setdefault("KBC_DATA_TYPE_SUPPORT", "authoritative")

_CASES = get_test_cases(str(_FUNCTIONAL_DIR))


def _seeded_state(case_dir: Path) -> dict | None:
    """Read a committed ``source/data/in/state.json``, if any.

    The datadirtest runner resets the working ``in/state.json`` to ``{}`` on every run unless an
    explicit ``last_state_override`` is supplied — so an incremental case's seeded watermark has to
    be passed through the runner, not just left on disk.
    """
    state_file = case_dir / "source" / "data" / "in" / "state.json"
    if not state_file.exists():
        return None
    content = json.loads(state_file.read_text())
    return content or None


# Incremental cases write a new ``last_run`` watermark to out/state.json; the datadirtest file
# comparison only diffs out/tables + out/files, so the watermark is asserted here directly. The
# fact that the cassette URI carries ``since`` is already enforced by VCR query matching on replay
# (a request without ``since`` would fail to match the recorded interaction).
_INCREMENTAL_CASES = {"06_run_issues_incremental_seeded", "07_run_issues_incremental_firstrun"}


@pytest.mark.skipif(not _CASES, reason="no functional cassettes present")
@pytest.mark.parametrize("case", _CASES)
def test_functional_case(case: str) -> None:
    """Replay one functional case and assert output / exit code / sync-action result match."""
    case_dir = _FUNCTIONAL_DIR / case
    test = VCRTestDataDir(
        data_dir=str(case_dir),
        component_script=str(_COMPONENT_SCRIPT),
        last_state_override=_seeded_state(case_dir),
        vcr_mode="replay",
        validate_snapshot=False,
        verbose=True,
    )
    test.setUp()
    try:
        test.compare_source_and_expected()
        if case in _INCREMENTAL_CASES:
            test._collect_result_state()
            # State is now a per-target watermark map: {"targets": {<repo>: <iso>}}.
            watermarks = [v for v in (test.result_state.get("targets") or {}).values() if v]
            assert watermarks, f"{case}: incremental run must write a non-empty per-target watermark"
            seeded = _seeded_state(case_dir) or {}
            assert all(w != seeded.get("last_run") for w in watermarks), (
                f"{case}: watermark must advance to the run start, not echo the seeded watermark"
            )
    finally:
        test.tearDown()
