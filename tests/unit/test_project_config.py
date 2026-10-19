"""SP-UNIVERSAL-PROJECT-CONFIG G1, G4–G6, AC4, AC6, AC7: ``repoach.toml``.

Pins the golden defaults (a repository without the file behaves as
repoach did on 2026-10-18), the validation of every key with a message
naming the file, key and line, the precedence environment > file >
origin > default, the spec resolution order, and the ``config show`` /
``doctor`` surfaces.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import repoach.core.config as core_config
from repoach.cli.main import app
from repoach.core.config import Settings
from repoach.project.config import (
    DEFAULT_LINT_COMMAND,
    DEFAULT_TEST_COMMAND,
    ConfigError,
    ProjectConfig,
    load_project_config,
    parse_project_config,
)
from repoach.project.measure import census, layout, measure_defaults
from repoach.project.resolve import effective_rows, resolve_project_config
from repoach.project.spec_source import resolve_spec, resolve_spec_id, spec_coverage_label
from repoach.project.toml_write import render_project_toml
from repoach.review import coder_loop
from repoach.review.plan import plan_relpath
from repoach.review.spec import SPECS_DIR, specs_dir_for

ENV_NAMES = (
    "REPOACH_FORGE",
    "REPOACH_INTEGRATION_BRANCH",
    "INTEGRATION_BRANCH",
    "REPOACH_RELEASE_BRANCH",
    "RELEASE_BRANCH",
    "REPOACH_MERGE_METHOD",
    "REPOACH_REMOVE_SOURCE_BRANCH",
    "REPOACH_REVIEW_BOT_LOGIN",
    "REVIEW_BOT_LOGIN",
    "REPOACH_PROVIDER",
    "REPOACH_DB_PATH",
    "REPOACH_LOGS_DIR",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    core_config._settings = None


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _repo(tmp_path: Path, origin: str | None = None, toml: str | None = None) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    if origin is not None:
        _git(root, "remote", "add", "origin", origin)
    if toml is not None:
        (root / "repoach.toml").write_text(toml, encoding="utf-8")
    return root


class TestGoldenDefaults:
    def test_no_file_equals_the_bare_model(self, tmp_path: Path) -> None:
        assert load_project_config(tmp_path) == ProjectConfig()

    def test_every_default_matches_the_literal_it_replaces(self, tmp_path: Path) -> None:
        config = ProjectConfig()
        assert config.repoach.specs_dir == str(SPECS_DIR) == "docs/specs"
        assert specs_dir_for(tmp_path) == tmp_path / "docs/specs"
        assert config.repoach.plans_dir == "docs/plans"
        assert plan_relpath("SP-X") == "docs/plans/SP-X.md"
        assert plan_relpath("SP-X", root=tmp_path) == "docs/plans/SP-X.md"
        assert config.repoach.merge_method == "squash"
        assert config.repoach.remove_source_branch is True
        assert config.repoach.integration_branch == "develop"
        assert (
            config.repoach.integration_branch == Settings.model_fields["integration_branch"].default
        )
        assert config.repoach.release_branch == "main"
        assert config.repoach.release_branch == Settings.model_fields["release_branch"].default
        own = config.with_host_defaults(repoach_host=True).commands
        assert own.lint == DEFAULT_LINT_COMMAND
        assert own.lint == "ruff check src tests && ruff format --check src tests"
        assert own.test == DEFAULT_TEST_COMMAND == "pytest -q tests/unit"
        foreign = config.with_host_defaults(repoach_host=False).commands
        assert foreign.lint is None and foreign.test is None
        assert config.coder.forbidden_prefixes == []
        assert coder_loop.FORBIDDEN_PREFIXES == (
            ".github/",
            ".githooks/",
            "prompts/review/",
            ".git/",
        )
        assert config.review.spec_sources == ["flag", "description", "branch"]
        assert config.arch.frontier_suppress == []
        assert config.state.db_path == "" and config.state.logs_dir == ""
        assert config.provider.kind == ""

    def test_without_a_file_the_forge_falls_back_to_github(self, tmp_path: Path) -> None:
        resolved = resolve_project_config(_repo(tmp_path))
        assert resolved.forge == "github"
        assert resolved.source("repoach.forge") == "default"


class TestValidation:
    @pytest.mark.parametrize(
        ("toml", "key", "line", "reason"),
        [
            ("[repoach]\nschema = 1\nfrge = 'gitlab'\n", "repoach.frge", 3, "unknown key"),
            ("[repoach]\nmerge_method = 'fast'\n", "repoach.merge_method", 2, "squash"),
            ("[repoach]\nforge = 'gitea'\n", "repoach.forge", 2, "github"),
            ("[repoach]\nremove_source_branch = 'maybe'\n", "repoach.remove_source_branch", 2, ""),
            ("[project]\nlanguages = 'python'\n", "project.languages", 2, "list"),
            ("[provider]\nkind = 'anthropic'\napi_key = 'sk-x'\n", "provider.api_key", 3, "secret"),
            ("[review]\ngitlab_token = 'x'\n", "review.gitlab_token", 2, "secret"),
            ("[repoach]\nschema = 2\n", "repoach.schema", 2, "newer"),
            ("[repoach]\nspecs_dir = '/etc'\n", "repoach.specs_dir", 2, "relative"),
            ("[state]\ndb_path = '../elsewhere.db'\n", "state.db_path", 2, "relative"),
            ("[state]\ndb_path = '~/ledger.db'\n", "state.db_path", 2, "'~'"),
            ("[state]\nlogs_dir = '~root/logs'\n", "state.logs_dir", 2, "relative"),
            ("[review]\nspec_sources = ['flag', 'title']\n", "review.spec_sources", 2, ""),
            ("[nosuch]\nx = 1\n", "nosuch", None, "unknown key"),
        ],
    )
    def test_invalid_key_names_file_key_and_line(
        self, tmp_path: Path, toml: str, key: str, line: int | None, reason: str
    ) -> None:
        (tmp_path / "repoach.toml").write_text(toml, encoding="utf-8")
        with pytest.raises(ConfigError) as info:
            load_project_config(tmp_path)
        error = info.value
        assert error.key == key
        assert error.line == line
        assert str(tmp_path / "repoach.toml") in str(error)
        assert f"key {key!r}" in str(error)
        assert reason in error.reason

    def test_unparsable_toml_reports_its_position(self, tmp_path: Path) -> None:
        (tmp_path / "repoach.toml").write_text("[repoach]\nforge = \n", encoding="utf-8")
        with pytest.raises(ConfigError) as info:
            load_project_config(tmp_path)
        assert info.value.line == 2
        assert "not valid TOML" in str(info.value)

    def test_conventions_section_is_read_but_inert(self, tmp_path: Path) -> None:
        text = "[conventions]\ntypes = ['feat', 'fix']\nheader_max_chars = 50\n"
        config = parse_project_config(text, tmp_path / "repoach.toml")
        assert config.conventions.types == ["feat", "fix"]
        assert config.conventions.header_max_chars == 50
        assert config.is_set("conventions.types")


class TestPrecedence:
    def test_forge_env_beats_file_beats_origin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _repo(tmp_path, "git@gitlab.com:o/r.git")
        assert resolve_project_config(root).forge == "gitlab"
        assert resolve_project_config(root).source("repoach.forge") == "origin"

        (root / "repoach.toml").write_text("[repoach]\nforge = 'github'\n", encoding="utf-8")
        assert resolve_project_config(root).forge == "github"
        assert resolve_project_config(root).source("repoach.forge") == "file"

        monkeypatch.setenv("REPOACH_FORGE", "gitlab")
        assert resolve_project_config(root).forge == "gitlab"
        assert resolve_project_config(root).source("repoach.forge") == "env"

    def test_bad_env_value_names_the_variable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REPOACH_MERGE_METHOD", "fast")
        with pytest.raises(ConfigError, match="REPOACH_MERGE_METHOD"):
            resolve_project_config(_repo(tmp_path))

    def test_merge_preferences_follow_env_over_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _repo(tmp_path, toml="[repoach]\nmerge_method = 'merge'\n")
        resolved = resolve_project_config(root)
        assert resolved.config.repoach.merge_method == "merge"
        monkeypatch.setenv("REPOACH_MERGE_METHOD", "rebase")
        monkeypatch.setenv("REPOACH_REMOVE_SOURCE_BRANCH", "false")
        resolved = resolve_project_config(root)
        assert resolved.config.repoach.merge_method == "rebase"
        assert resolved.config.repoach.remove_source_branch is False

    def test_settings_read_the_file_below_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _repo(
            tmp_path,
            toml="[repoach]\nintegration_branch = 'trunk'\n\n[state]\ndb_path = 'var/ledger.db'\n",
        )
        monkeypatch.chdir(root)
        settings = Settings()
        assert settings.integration_branch == "trunk"
        assert settings.db_path == (root / "var/ledger.db").resolve()

        monkeypatch.setenv("INTEGRATION_BRANCH", "next")
        monkeypatch.setenv("REPOACH_DB_PATH", str(tmp_path / "env.db"))
        settings = Settings()
        assert settings.integration_branch == "next"
        assert settings.db_path == tmp_path / "env.db"

    def test_user_env_file_outranks_the_project_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
        root = _repo(tmp_path, toml="[repoach]\nrelease_branch = 'stable'\n")
        user_env = Path(core_config.env_files(root)[0])
        user_env.parent.mkdir(parents=True, exist_ok=True)
        user_env.write_text("REPOACH_RELEASE_BRANCH=prod\n", encoding="utf-8")
        monkeypatch.chdir(root)
        assert Settings().release_branch == "prod"
        assert resolve_project_config(root).source("repoach.release_branch") == "env"

    def test_bot_username_env_beats_file_beats_forge_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _repo(tmp_path, "git@gitlab.com:o/r.git")
        rows = {
            key: (value, source)
            for key, value, source in effective_rows(resolve_project_config(root))
        }
        assert rows["review.bot_username"] == ("", "default")
        (root / "repoach.toml").write_text("[review]\nbot_username = 'project_1_bot'\n")
        rows = {
            key: (value, source)
            for key, value, source in effective_rows(resolve_project_config(root))
        }
        assert rows["review.bot_username"] == ("project_1_bot", "file")
        monkeypatch.setenv("REPOACH_REVIEW_BOT_LOGIN", "token-user")
        rows = {
            key: (value, source)
            for key, value, source in effective_rows(resolve_project_config(root))
        }
        assert rows["review.bot_username"] == ("token-user", "env")

    def test_configured_spec_and_plan_dirs_are_used(self, tmp_path: Path) -> None:
        (tmp_path / "repoach.toml").write_text(
            "[repoach]\nspecs_dir = 'design/specs'\nplans_dir = 'design/plans'\n"
        )
        assert specs_dir_for(tmp_path) == tmp_path / "design/specs"
        assert plan_relpath("SP-X", root=tmp_path) == "design/plans/SP-X.md"


class TestTemplate:
    def test_every_key_has_a_template_line_and_round_trips(self, tmp_path: Path) -> None:
        values: dict[str, object] = {}
        for section, keys in ProjectConfig().model_dump(by_alias=True).items():
            if section == "conventions":
                continue
            for key, value in keys.items():
                if value is not None:
                    values[f"{section}.{key}"] = value
        text = render_project_toml(values)
        assert parse_project_config(text, tmp_path / "repoach.toml") == ProjectConfig()

    def test_untouched_template_parses_to_defaults(self, tmp_path: Path) -> None:
        text = render_project_toml({})
        assert parse_project_config(text, tmp_path / "repoach.toml") == ProjectConfig()

    def test_unknown_key_is_refused(self) -> None:
        with pytest.raises(KeyError):
            render_project_toml({"repoach.nope": "x"})


class TestMeasure:
    def test_census_ranks_by_file_count_and_drops_minor_languages(self) -> None:
        files = ["a.sv", "b/c.tsx", "d.py", "e.sh", "README.md", "f/g.go"]
        assert census(files) == ["python", "typescript", "go", "verilog", "shell"]
        gum = [f"x{i}.go" for i in range(59)] + [f"s{i}.sh" for i in range(11)]
        gum += ["examples/gum.py", "examples/gum.js"] + [f"doc{i}.md" for i in range(43)]
        assert census(gum) == ["go", "shell"]
        lone = ["tool.py"] + [f"doc{i}.md" for i in range(99)]
        assert census(lone) == ["python"]
        two_minor = ["a.py", "b.sh"] + [f"doc{i}.md" for i in range(198)]
        assert census(two_minor) == ["python"]

    def test_layout_reads_top_level_directories(self) -> None:
        files = ["src/a.py", "tests/test_a.py", "docs/x.py", ".github/y.py", "api/b.py"]
        assert layout(files) == (["api", "src"], ["tests"])
        assert layout(["main.go", "cmd/x.go"]) == (["."], [])

    def test_repoach_itself_measures_the_golden_commands(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        for relpath in ("pyproject.toml", "src/repoach/a.py", "tests/unit/test_a.py"):
            target = root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n", encoding="utf-8")
        _git(root, "add", "-A")
        values = measure_defaults(root, probe=False).values
        assert values["commands.lint"] == DEFAULT_LINT_COMMAND
        assert values["commands.test"] == DEFAULT_TEST_COMMAND
        assert values["project.languages"] == ["python"]


class TestSpecResolution:
    def test_flag_beats_description_beats_branch(self, tmp_path: Path) -> None:
        body = "Adds the MR helper.\n\nSpec: SP-YOOB-MR-CREATE\n"
        flag = resolve_spec(
            flag="sp-other", description=body, branch="feat/sp-x-impl", root=tmp_path
        )
        assert (flag.spec_id, flag.source) == ("SP-OTHER", "flag")
        described = resolve_spec(
            flag=None, description=body, branch="feat/sp-x-impl", root=tmp_path
        )
        assert (described.spec_id, described.source) == ("SP-YOOB-MR-CREATE", "description")
        branch = resolve_spec(flag=None, description="", branch="feat/sp-x-impl", root=tmp_path)
        assert (branch.spec_id, branch.source) == ("SP-X", "branch")

    def test_no_declared_spec_resolves_to_none(self, tmp_path: Path) -> None:
        none = resolve_spec(flag=None, description="docs only", branch="docs/readme", root=tmp_path)
        assert (none.spec_id, none.source) == (None, None)
        assert spec_coverage_label(none.spec_id, None) == "not_applicable"

    def test_spec_line_must_be_a_whole_line(self) -> None:
        assert resolve_spec_id(flag=None, description="see Spec: SP-A", branch="") is None
        assert resolve_spec_id(flag=None, description="Spec: SP-A extra", branch="") is None
        assert resolve_spec_id(flag=None, description="x\nSpec:  SP-A-1  \n", branch="") == "SP-A-1"

    def test_spec_key_is_case_insensitive_and_the_id_verbatim(self) -> None:
        for line in ("spec: SP-Yoob-1", "SPEC: SP-Yoob-1", "Spec:SP-Yoob-1"):
            assert resolve_spec_id(flag=None, description=line, branch="") == "SP-Yoob-1"
        assert resolve_spec_id(flag=None, description="Spec: sp-x.2", branch="") == "sp-x.2"

    def test_sources_restrict_the_order(self, tmp_path: Path) -> None:
        found = resolve_spec(
            flag=None,
            description="Spec: SP-DESC",
            branch="feat/sp-branch-impl",
            sources=("branch",),
            root=tmp_path,
        )
        assert (found.spec_id, found.source) == ("SP-BRANCH", "branch")


def test_repoach_own_config_is_tagged_file_or_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC7: repoach's committed ``repoach.toml`` resolves with no env or origin source."""
    checkout = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(checkout)
    result = CliRunner().invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.output
    sources = {
        line.rsplit("# source: ", 1)[1] for line in result.stdout.splitlines() if " = " in line
    }
    assert sources == {"file", "default"}
    resolved = resolve_project_config(checkout)
    assert resolved.forge == "github"
    assert resolved.config.repoach.merge_method == "squash"
    assert resolved.config.provider.kind == "gateway"


class TestGatesKeys:
    @pytest.mark.parametrize(
        ("toml", "key", "reason"),
        [
            ("[commands]\ntimeout_s = 0\n", "commands.timeout_s", "greater than 0"),
            ("[commands]\ntimeout_s = true\n", "commands.timeout_s", "integer"),
            ("[commands]\ne2e = ''\n", "commands", "commands.e2e"),
            ("[commands]\ne2e = 3\n", "commands", "commands.e2e"),
            ("[project]\nprompts_dir = '/srv/prompts'\n", "project.prompts_dir", "relative"),
            ("[gates]\nverifier_commands = {ruff = 1}\n", "gates.verifier_commands", "string"),
            ("[gates]\nadvisory = ['x']\n", "gates.advisory", "unknown key"),
            ("[arch]\nrequire_frontmatter = 'yes'\n", "arch.require_frontmatter", "boolean"),
        ],
    )
    def test_new_keys_are_validated(self, tmp_path: Path, toml: str, key: str, reason: str) -> None:
        (tmp_path / "repoach.toml").write_text(toml, encoding="utf-8")
        with pytest.raises(ConfigError) as info:
            load_project_config(tmp_path)
        assert info.value.key == key
        assert reason in str(info.value)

    def test_named_commands_and_gates_keys_are_read(self, tmp_path: Path) -> None:
        text = (
            "[commands]\ntimeout_s = 60\ne2e = 'npm run e2e'\n\n"
            "[gates]\nadvisory_verifiers = ['docstring']\ndisabled_verifiers = ['import']\n"
            "verifier_commands = {docstring = 'pydocstyle src'}\n"
        )
        config = parse_project_config(text, tmp_path / "repoach.toml")
        assert config.commands.timeout_s == 60
        assert config.commands.named() == {"e2e": "npm run e2e"}
        assert config.is_set("commands.e2e")
        assert config.gates.advisory_verifiers == ["docstring"]
        assert config.gates.disabled_verifiers == ["import"]
        assert config.gates.verifier_commands == {"docstring": "pydocstyle src"}

    def test_host_defaults_differ_between_repoach_and_a_foreign_repo(self, tmp_path: Path) -> None:
        foreign = resolve_project_config(_repo(tmp_path)).config
        assert foreign.arch.require_frontmatter is False
        assert foreign.gates.docstring_exempt == ["tests/"]
        assert foreign.project.prompts_dir == "prompts/review"
        assert foreign.commands.timeout_s == 900

        checkout = Path(__file__).resolve().parents[2]
        own = ProjectConfig().with_host_defaults(repoach_host=True)
        assert own.arch.require_frontmatter is True
        assert own.gates.docstring_exempt == ["tests/", "scripts/", "src/repoach/llm_proxy/"]
        assert resolve_project_config(checkout).config.arch.require_frontmatter is True


class TestAsMapping:
    def test_mapping_is_toml_shaped_and_resolved(self, tmp_path: Path) -> None:
        root = _repo(
            tmp_path,
            "git@gitlab.com:o/r.git",
            "[repoach]\nschema = 1\n\n[commands]\nlint = 'npm run lint'\ne2e = 'npm run e2e'\n",
        )
        mapping = resolve_project_config(root).config.as_mapping()
        assert list(mapping) == [
            "repoach",
            "project",
            "commands",
            "coder",
            "provider",
            "review",
            "gates",
            "arch",
            "state",
            "conventions",
        ]
        assert mapping["repoach"]["schema"] == 1
        assert mapping["repoach"]["forge"] == "gitlab"
        assert mapping["commands"]["e2e"] == "npm run e2e"
        assert mapping["commands"]["lint"] == "npm run lint"
        assert mapping["commands"]["timeout_s"] == 900
        assert mapping["gates"]["docstring_exempt"] == ["tests/"]
        assert mapping["arch"]["require_frontmatter"] is False
        assert mapping["project"]["prompts_dir"] == "prompts/review"

    def test_mapping_round_trips_through_the_parser(self, tmp_path: Path) -> None:
        config = resolve_project_config(_repo(tmp_path)).config
        assert ProjectConfig.model_validate(config.as_mapping()) == config

    def test_unresolved_none_keys_are_omitted(self) -> None:
        mapping = ProjectConfig().as_mapping()
        assert "docstring_exempt" not in mapping["gates"]
        assert "require_frontmatter" not in mapping["arch"]
        assert "forge" not in mapping["repoach"]


def test_undeclared_commands_are_skipped_with_a_reason_on_a_foreign_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path, "https://github.com/o/r.git", "[commands]\ngate = 'make check'\n")
    monkeypatch.chdir(root)
    mapping = resolve_project_config(root).config.as_mapping()
    assert "lint" not in mapping["commands"] and "test" not in mapping["commands"]
    assert mapping["commands"]["gate"] == "make check"
    shown = CliRunner().invoke(app, ["config", "show"]).stdout
    assert "# lint: not declared in repoach.toml; skipped on this repository" in shown
    doctor = CliRunner().invoke(app, ["doctor"]).stdout
    assert (
        "config.commands.test=<not declared in repoach.toml; skipped on this repository>" in doctor
    )
