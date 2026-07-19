"""Integration test for SP-REFUTED-FEEDBACK — orchestrator wires db_path into reviewers.

Exercises the full end-to-end flow: seed a temporary ledger with refuted
findings for one lens, construct a reviewer with db_path pointing at it,
capture the rendered prompt, and assert the track-record section appears
(and does not appear for a lens with no refutations).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
    update_finding_status,
)
from repoach.review.reviewer import Scribe, Sentinel


@pytest.fixture(autouse=True)
def _stub_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep reviewer construction possible without the operator ``.env``.

    SP-PROXY-SECURE-DEFAULTS removed the implicit token fallback, so a
    tokenless environment (CI) refuses to build the underlying
    AgentLoop — this flow targets db_path wiring, not auth.
    """
    monkeypatch.setattr(
        "repoach.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://localhost:8082",
            llm_proxy_auth_token=SecretStr("test-token"),
        ),
    )


@pytest.fixture()
def tmp_ledger_with_sentinel_refutations() -> Path:
    """Create a temporary ledger with two refuted findings for 'sentinel'.

    The ledger also contains one VERIFIED finding for sentinel so
    the precision line is non-trivial (1 verified, 2 refuted).
    """
    tmp = Path(tempfile.mkdtemp()) / "test_findings.db"
    init_findings_schema(tmp)

    f_verified = Finding(
        pr_number=1,
        head_sha="abc123",
        round=1,
        finder="sentinel",
        claim_type=ClaimType.SECURITY,
        severity=Severity.BLOCKING,
        file="src/repoach/app.py",
        line_start=5,
        line_end=5,
        claim="Unvalidated redirect in login handler",
        evidence_pointer="redirect param used directly",
        status=FindingStatus.PROPOSED,
    )
    fv_id = record_finding(tmp, f_verified)
    update_finding_status(
        tmp,
        fv_id,
        FindingStatus.VERIFIED,
        verification_method="manual_review",
        verification_result="Confirmed: redirect target is unsanitised",
    )

    f1 = Finding(
        pr_number=1,
        head_sha="abc123",
        round=1,
        finder="sentinel",
        claim_type=ClaimType.SECURITY,
        severity=Severity.BLOCKING,
        file="src/repoach/app.py",
        line_start=10,
        line_end=10,
        claim="Hardcoded secret in app.py",
        evidence_pointer="line 10 shows ghp_xxx",
        status=FindingStatus.PROPOSED,
    )
    fid1 = record_finding(tmp, f1)
    update_finding_status(
        tmp,
        fid1,
        FindingStatus.REFUTED,
        verification_method="ast_verifier",
        verification_result="The token is loaded from env, not hardcoded",
    )

    f2 = Finding(
        pr_number=2,
        head_sha="def456",
        round=1,
        finder="sentinel",
        claim_type=ClaimType.SECURITY,
        severity=Severity.ADVISORY,
        file="src/repoach/utils.py",
        line_start=42,
        line_end=42,
        claim="eval() usage without sanitisation",
        evidence_pointer="line 42 uses eval(user_input)",
        status=FindingStatus.PROPOSED,
    )
    fid2 = record_finding(tmp, f2)
    update_finding_status(
        tmp,
        fid2,
        FindingStatus.REFUTED,
        verification_method="ast_verifier",
        verification_result="Input is validated upstream via strict regex",
    )

    return tmp


def test_orchestrator_wires_db_path_to_reviewers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_ledger_with_sentinel_refutations: Path,
) -> None:
    """Seed ledger → build reviewer → capture prompt → assert track record.

    Sentinel gets a ledger with its own refuted findings, so its prompt
    must carry the fixed heading and the refuted claims.  Scribe gets the
    same ledger but has no refutations there, so its prompt must omit the
    heading.
    """
    captured: dict[str, str] = {}

    def _fake_call_sentinel(self: Sentinel, prompt: str, *, pr_number: int | None = None) -> Any:
        captured["sentinel_prompt"] = prompt
        raise RuntimeError("stop after prompt capture")

    monkeypatch.setattr(Sentinel, "_call_with_retry", _fake_call_sentinel)

    with pytest.raises(RuntimeError):
        Sentinel(db_path=tmp_ledger_with_sentinel_refutations).review_diff("x")

    prompt = captured["sentinel_prompt"]
    assert "Your recent refuted claims — do not re-raise without new evidence" in prompt
    assert "Hardcoded secret in app.py" in prompt
    assert "eval() usage without sanitisation" in prompt
    assert "refuter: The token is loaded from env, not hardcoded" in prompt
    assert "refuter: Input is validated upstream via strict regex" in prompt

    captured2: dict[str, str] = {}

    def _fake_call_scribe(self: Scribe, prompt: str, *, pr_number: int | None = None) -> Any:
        captured2["scribe_prompt"] = prompt
        raise RuntimeError("stop after prompt capture")

    monkeypatch.setattr(Scribe, "_call_with_retry", _fake_call_scribe)

    with pytest.raises(RuntimeError):
        Scribe(db_path=tmp_ledger_with_sentinel_refutations).review_diff("x")

    assert (
        "Your recent refuted claims — do not re-raise without new evidence"
        not in captured2["scribe_prompt"]
    )
