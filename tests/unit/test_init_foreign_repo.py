"""SP-UNIVERSAL-PROJECT-CONFIG G2/G3, AC1–AC3, AC5: ``repoach init`` on a foreign repository.

Every test drives the real CLI against a real git repository under
``tmp_path``: a TypeScript-only tree with no Python at all, and a
scratch replica of Yoob's layout (``pyproject.toml`` at the root, a
``frontend/`` TypeScript app, Verilog, ``.gitlab-ci.yml``, its own
``core.hooksPath = scripts/hooks``, a ``.githooks/`` directory and a
``.env``).  ``init`` must write exactly ``repoach.toml``, the spec
template and one ``.gitignore`` line, and leave every other byte —
``.git/config`` included — identical.
"""

from __future__ import annotations

import hashlib
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.cli.main import app
from repoach.core.resources import spec_template_path

YOOB_ORIGIN = "git@gitlab.com:josephwfaye/yoob_2.git"


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for relpath, content in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _commit_all(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "init")


def _porcelain(root: Path) -> set[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True
    )
    return {line for line in completed.stdout.splitlines() if line}


def _fingerprint(root: Path, skip: set[str]) -> dict[str, str]:
    """Hash every file under ``root`` (``.git/`` included) except ``skip``."""
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relpath = path.relative_to(root).as_posix()
        if path.is_file() and relpath not in skip and not relpath.startswith(".git/objects/"):
            digests[relpath] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _init(root: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, str, str]:
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["init", *args])
    return result.exit_code, result.stdout, result.stderr


@pytest.fixture
def typescript_repo(tmp_path: Path) -> Path:
    """A repository with no Python file at all, GitHub origin, no remote refs."""
    root = tmp_path / "widget"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "remote", "add", "origin", "https://github.com/acme/widget.git")
    _write(
        root,
        {
            "src/index.ts": "export const answer = 42;\n",
            "src/view.tsx": "export const View = () => null;\n",
            "tests/index.test.ts": "import { answer } from '../src/index';\n",
            "package.json": '{"name": "widget", "scripts": {"lint": "eslint .", "test": "vitest"}}\n',
            ".gitignore": "node_modules/\n",
        },
    )
    _commit_all(root)
    return root


@pytest.fixture
def yoob_layout(tmp_path: Path) -> Path:
    """A scratch clone shaped like Yoob, cloned from a local bare remote.

    The bare remote carries ``main`` (its default branch) and
    ``develop``; after the clone ``origin`` is pointed at Yoob's GitLab
    URL, exactly what a developer's clone looks like, without network.
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _write(
        seed,
        {
            "pyproject.toml": '[project]\nname = "yoob"\nversion = "1.0.0"\n',
            "src/yoob/__init__.py": '"""Yoob."""\n',
            "tests/test_smoke.py": "def test_smoke() -> None:\n    assert True\n",
            "api/main.py": '"""API."""\n',
            "frontend/package.json": '{"scripts": {"lint": "eslint ."}}\n',
            "frontend/src/App.tsx": "export const App = () => null;\n",
            "rtl/top.v": "module top; endmodule\n",
            ".gitlab-ci.yml": "stages: [test]\n",
            "scripts/hooks/commit-msg": "#!/bin/sh\nexit 0\n",
            "ci/run-local-ci.sh": "#!/usr/bin/env bash\nexit 0\n",
            ".githooks/pre-commit": "#!/bin/sh\nexit 0\n",
            ".gitignore": "__pycache__/\n.env\n",
        },
    )
    _commit_all(seed)
    _git(seed, "branch", "develop")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    clone = tmp_path / "yoob_2"
    _git(tmp_path, "clone", "-q", str(bare), str(clone))
    _git(clone, "remote", "set-url", "origin", YOOB_ORIGIN)
    _git(clone, "config", "core.hooksPath", "scripts/hooks")
    (clone / ".env").write_text("YOOB_SECRET=keep-me\n", encoding="utf-8")
    return clone


def test_dry_run_writes_nothing(typescript_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _fingerprint(typescript_repo, set())

    code, out, _ = _init(typescript_repo, monkeypatch, "--dry-run", "--offline")

    assert code == 0
    assert out.splitlines()[:3] == [
        "init: would create repoach.toml",
        "init: would create docs/specs/_TEMPLATE.md",
        "init: would update .gitignore (+ .repoach.env)",
    ]
    assert _porcelain(typescript_repo) == set()
    assert _fingerprint(typescript_repo, set()) == before


def test_typescript_repo_gets_exactly_two_files_and_one_gitignore_line(
    typescript_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks_before = subprocess.run(
        ["git", "config", "core.hooksPath"], cwd=typescript_repo, capture_output=True, text=True
    ).stdout
    code, out, _ = _init(typescript_repo, monkeypatch, "--offline")

    assert code == 0, out
    assert "init: created repoach.toml" in out
    assert "init: created docs/specs/_TEMPLATE.md" in out
    assert _porcelain(typescript_repo) == {" M .gitignore", "?? repoach.toml", "?? docs/"}
    assert _git(typescript_repo, "diff", "--", ".gitignore").splitlines()[-1] == "+.repoach.env"
    hooks_after = subprocess.run(
        ["git", "config", "core.hooksPath"], cwd=typescript_repo, capture_output=True, text=True
    ).stdout
    assert hooks_after == hooks_before == ""
    assert not (typescript_repo / ".env").exists()

    config = tomllib.loads((typescript_repo / "repoach.toml").read_text(encoding="utf-8"))
    assert config["repoach"]["forge"] == "github"
    assert config["repoach"]["merge_method"] == "squash"
    assert config["project"]["languages"] == ["typescript"]
    assert config["project"]["src_roots"] == ["src"]
    assert config["project"]["test_roots"] == ["tests"]
    assert config["commands"] == {"lint": "npm run lint", "test": "npm test"}
    assert (typescript_repo / "docs/specs/_TEMPLATE.md").read_bytes() == (
        spec_template_path().read_bytes()
    )


def test_rerun_is_byte_identical_and_says_unchanged(
    typescript_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(typescript_repo, monkeypatch, "--offline")
    after_first = _fingerprint(typescript_repo, set())

    code, out, _ = _init(typescript_repo, monkeypatch, "--offline")

    assert code == 0
    assert out.splitlines() == [
        "init: repoach.toml unchanged",
        "init: docs/specs/_TEMPLATE.md unchanged",
        "init: .gitignore unchanged",
    ]
    assert _fingerprint(typescript_repo, set()) == after_first


def test_languages_line_appears_once(
    typescript_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(typescript_repo, monkeypatch, "--offline")
    text = (typescript_repo / "repoach.toml").read_text(encoding="utf-8")
    assert sum("languages" in line for line in text.splitlines()) == 1


def test_yoob_layout_is_byte_identical_except_the_init_files(
    yoob_layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = {"repoach.toml", "docs/specs/_TEMPLATE.md", ".gitignore"}
    before = _fingerprint(yoob_layout, written | {".git/index"})
    gitignore_before = (yoob_layout / ".gitignore").read_text(encoding="utf-8")

    code, out, _ = _init(yoob_layout, monkeypatch, "--offline")

    assert code == 0, out
    assert _porcelain(yoob_layout) == {" M .gitignore", "?? repoach.toml", "?? docs/"}
    assert _fingerprint(yoob_layout, written | {".git/index"}) == before
    assert (yoob_layout / ".gitignore").read_text(encoding="utf-8") == (
        gitignore_before + ".repoach.env\n"
    )
    assert _git(yoob_layout, "config", "core.hooksPath") == "scripts/hooks"
    assert (yoob_layout / ".env").read_text(encoding="utf-8") == "YOOB_SECRET=keep-me\n"

    config = tomllib.loads((yoob_layout / "repoach.toml").read_text(encoding="utf-8"))
    assert config["repoach"]["forge"] == "gitlab"
    assert config["repoach"]["integration_branch"] == "develop"
    assert config["repoach"]["release_branch"] == "main"
    assert {"python", "typescript", "verilog"} <= set(config["project"]["languages"])
    assert config["project"]["test_roots"] == ["tests"]
    assert config["commands"]["test"] == "pytest -q tests"


def test_merge_method_is_read_from_the_forge_api(
    yoob_layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: Yoob's GitLab project answers ``merge_method: merge``."""
    calls: list[list[str]] = []

    def fake_run_json(argv: list[str], cwd: Path) -> dict[str, object] | None:
        calls.append(argv)
        return {"merge_method": "merge", "squash_option": "default_off"}

    monkeypatch.setattr("repoach.project.measure._run_json", fake_run_json)
    monkeypatch.setattr("repoach.project.measure.shutil.which", lambda name: f"/usr/bin/{name}")

    code, out, _ = _init(yoob_layout, monkeypatch)

    assert code == 0, out
    config = tomllib.loads((yoob_layout / "repoach.toml").read_text(encoding="utf-8"))
    assert config["repoach"]["merge_method"] == "merge"
    assert calls == [["glab", "api", "projects/josephwfaye%2Fyoob_2", "--hostname", "gitlab.com"]]


def test_unreadable_forge_api_keeps_squash(
    yoob_layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("repoach.project.measure._run_json", lambda argv, cwd: None)
    monkeypatch.setattr("repoach.project.measure.shutil.which", lambda name: f"/usr/bin/{name}")

    code, _, _ = _init(yoob_layout, monkeypatch)

    assert code == 0
    config = tomllib.loads((yoob_layout / "repoach.toml").read_text(encoding="utf-8"))
    assert config["repoach"]["merge_method"] == "squash"


def test_no_gitignore_is_never_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, {"main.go": "package main\n", "go.mod": "module x\n"})
    _commit_all(root)

    code, out, _ = _init(root, monkeypatch, "--offline")

    assert code == 0
    assert not (root / ".gitignore").exists()
    assert _porcelain(root) == {"?? repoach.toml", "?? docs/"}
    assert "no origin remote" in out
    config = tomllib.loads((root / "repoach.toml").read_text(encoding="utf-8"))
    assert "forge" not in config["repoach"]
    assert config["project"]["languages"] == ["go"]
    assert config["project"]["src_roots"] == ["."]
    assert config["commands"] == {"lint": "go vet ./...", "test": "go test ./..."}


def test_existing_template_is_never_overwritten_even_with_force(
    typescript_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = typescript_repo / "docs/specs/_TEMPLATE.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("# our own template\n", encoding="utf-8")
    _init(typescript_repo, monkeypatch, "--offline")
    _commit_all(typescript_repo)

    code, out, _ = _init(typescript_repo, monkeypatch, "--offline", "--force")

    assert code == 0, out
    assert "init: docs/specs/_TEMPLATE.md unchanged" in out
    assert custom.read_text(encoding="utf-8") == "# our own template\n"


def test_force_refuses_uncommitted_changes_without_yes(
    typescript_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(typescript_repo, monkeypatch, "--offline")
    config_file = typescript_repo / "repoach.toml"
    config_file.write_text(config_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    edited = config_file.read_bytes()

    code, _, err = _init(typescript_repo, monkeypatch, "--offline", "--force")

    assert code == 2
    assert "uncommitted changes" in err
    assert config_file.read_bytes() == edited

    code, out, _ = _init(typescript_repo, monkeypatch, "--offline", "--force", "--yes")
    assert code == 0
    assert "init: rewrote repoach.toml" in out


def test_repoach_dev_is_refused_on_a_foreign_repository(
    yoob_layout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _, err = _init(yoob_layout, monkeypatch, "--repoach-dev")

    assert code == 2
    assert "not a repoach checkout" in err
    assert _git(yoob_layout, "config", "core.hooksPath") == "scripts/hooks"
    assert _porcelain(yoob_layout) == set()


def test_invalid_existing_config_exits_2(
    typescript_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (typescript_repo / "repoach.toml").write_text("[repoach]\nmerge_method = 'fast'\n")

    code, _, err = _init(typescript_repo, monkeypatch, "--offline")

    assert code == 2
    assert "repoach.toml:2" in err
    assert "repoach.merge_method" in err
