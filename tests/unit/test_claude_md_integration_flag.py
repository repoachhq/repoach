from pathlib import Path


def test_claude_md_integration_flag_documented() -> None:
    """Verify that the --integration flag is documented in CLAUDE.md."""
    claude_md_path = Path("CLAUDE.md")
    assert claude_md_path.exists(), "CLAUDE.md must exist"
    content = claude_md_path.read_text(encoding="utf-8")
    assert "scripts/ci_local.sh --integration" in content, (
        "The --integration flag must be documented in CLAUDE.md"
    )
