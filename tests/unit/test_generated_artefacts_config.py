"""SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ AC1: the ``[[review.generated]]`` declaration.

An entry loads into :class:`GeneratedEntry`; every shape the spec refuses
is a :class:`ConfigError` naming the key ``review.generated[<i>].<key>``
and the line it sits on.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from repoach.project.config import GENERATED_ENTRIES_MAX, ConfigError, parse_project_config
from repoach.project.globs import glob_matches
from repoach.review.generated_config import load_generated_entries, touched_artefacts
from repoach.review.generated_types import GeneratedEntry

_FILE = Path("repoach.toml")

_ENTRY = """[project]
languages = ["python"]

[[review.generated]]
path = "api/data/knowledge_embeddings.json"
check = ["scripts/index_knowledge_corpus.py", "--repo-root", ".", "--check"]
sources = ["api/data/knowledge_corpus.json", "docs/user_guide/**"]
producer_inputs = ["api/data/_header.json"]
"""


def _load(text: str, languages: tuple[str, ...] = ("python",)) -> tuple[GeneratedEntry, ...]:
    return load_generated_entries(text, _FILE, languages)


def test_one_entry_loads_into_generated_entry() -> None:
    """The documented entry loads field for field."""
    assert _load(_ENTRY) == (
        GeneratedEntry(
            path="api/data/knowledge_embeddings.json",
            check=("scripts/index_knowledge_corpus.py", "--repo-root", ".", "--check"),
            sources=("api/data/knowledge_corpus.json", "docs/user_guide/**"),
            producer_inputs=("api/data/_header.json",),
            check_timeout_s=120,
        ),
    )


def test_no_declaration_is_no_entry() -> None:
    """A file without the table declares nothing."""
    assert _load('[project]\nlanguages = ["python"]\n') == ()


def _refused(text: str, languages: tuple[str, ...] = ("python",)) -> ConfigError:
    with pytest.raises(ConfigError) as caught:
        _load(text, languages)
    return caught.value


def test_string_check_is_refused() -> None:
    """``check`` as one command string is refused on its own line."""
    error = _refused(
        _ENTRY.replace(
            'check = ["scripts/index_knowledge_corpus.py", "--repo-root", ".", "--check"]',
            'check = "scripts/index_knowledge_corpus.py --check"',
        )
    )
    assert error.key == "review.generated[0].check"
    assert error.line == 6


@pytest.mark.parametrize("argument", ["--check;rm", "a|b", "a&&b", "--repo-root ."])
def test_shell_shaped_check_argument_is_refused(argument: str) -> None:
    """A ``;``, ``|``, ``&`` or space in a check argument is refused."""
    error = _refused(_ENTRY.replace('"--check"]', f'"{argument}"]'))
    assert error.key == "review.generated[0].check"
    assert error.line == 6
    assert "not a plain argument" in str(error)


def test_placeholders_are_the_only_braces_allowed() -> None:
    """``{head}`` and ``{base}`` pass; any other brace is refused."""
    entries = _load(_ENTRY.replace('"--check"]', '"--head={head}", "--base={base}"]'))
    assert entries[0].check[-2:] == ("--head={head}", "--base={base}")
    error = _refused(_ENTRY.replace('"--check"]', '"{other}"]'))
    assert error.key == "review.generated[0].check"


def test_empty_sources_is_refused() -> None:
    """An entry deriving from nothing is refused."""
    error = _refused(
        _ENTRY.replace(
            'sources = ["api/data/knowledge_corpus.json", "docs/user_guide/**"]', "sources = []"
        )
    )
    assert error.key == "review.generated[0].sources"
    assert error.line == 7


@pytest.mark.parametrize("path", ["../outside.json", "api/../../x.json", "/abs/x.json"])
def test_path_leaving_the_repository_is_refused(path: str) -> None:
    """A ``path`` with ``..`` or a leading ``/`` is refused."""
    error = _refused(_ENTRY.replace("api/data/knowledge_embeddings.json", path))
    assert error.key == "review.generated[0].path"
    assert error.line == 5


@pytest.mark.parametrize("glob", ["src/*", "**/*.py", "lib/**", "tools/gen?.py"])
def test_path_glob_matching_python_is_refused(glob: str) -> None:
    """A glob that can match a ``.py`` file (or any script) is refused."""
    error = _refused(_ENTRY.replace("api/data/knowledge_embeddings.json", glob))
    assert error.key == "review.generated[0].path"
    assert error.line == 5
    assert "can match" in str(error)


def test_code_glob_is_judged_against_the_declared_languages() -> None:
    """``*.go`` (not a script extension) is code only where Go is declared."""
    text = _ENTRY.replace('languages = ["python"]', 'languages = ["shell"]').replace(
        "api/data/knowledge_embeddings.json", "pkg/gen/*.go"
    )
    assert _load(text, languages=())[0].path == "pkg/gen/*.go"
    error = _refused(text, languages=("go",))
    assert error.key == "review.generated[0].path"
    assert ".go" in str(error)


def test_a_data_glob_beside_python_loads() -> None:
    """``data/*.npy`` can name neither code nor a manifest, so it loads with Python declared."""
    assert _load(_ENTRY.replace("api/data/knowledge_embeddings.json", "data/*.npy"))


def test_the_same_path_twice_is_refused() -> None:
    """Two entries declaring the same path name the second one."""
    second = _ENTRY.split("\n", 3)[3]
    error = _refused(_ENTRY + "\n" + second)
    assert error.key == "review.generated[1].path"
    assert error.line == 11


def test_two_globs_matching_one_changed_path_are_refused() -> None:
    """Two different globs matching the same changed path are refused when it changes."""
    text = (
        _ENTRY
        + "\n"
        + _ENTRY.split("\n", 3)[3].replace(
            "api/data/knowledge_embeddings.json", "api/data/*_embeddings.json"
        )
    )
    entries = _load(text)
    with pytest.raises(ConfigError, match="matched by two entries"):
        touched_artefacts(entries, ["api/data/knowledge_embeddings.json"])


def test_entries_are_capped() -> None:
    """More than ``generated_entries_max`` entries are refused."""
    block = '\n[[review.generated]]\npath = "d/{n}.json"\ncheck = ["s.sh"]\nsources = ["x"]\n'
    text = "".join(block.format(n=n) for n in range(GENERATED_ENTRIES_MAX + 1))
    with pytest.raises(ConfigError) as caught:
        parse_project_config(text, _FILE)
    assert caught.value.key == "review.generated"


def test_unknown_key_in_an_entry_is_refused() -> None:
    """An entry refuses unknown keys like every section."""
    error = _refused(_ENTRY + 'shell = "bash -c"\n')
    assert error.key == "review.generated[0].shell"
    assert error.line == 9
    assert "unknown key" in str(error)


def test_unterminated_bracket_is_a_literal() -> None:
    """A ``[`` with no closing ``]`` matches itself, never a class."""
    assert glob_matches("data/[weird.json", "data/[weird.json")
    assert not glob_matches("data/[weird.json", "data/w")


def test_negated_class_matches_the_complement() -> None:
    """``[!x]`` matches any one character but ``x``, never a slash."""
    assert glob_matches("data/v[!0].json", "data/v1.json")
    assert not glob_matches("data/v[!0].json", "data/v0.json")
    assert not glob_matches("data/v[!0].json", "data/v/.json")


def test_error_names_the_line_of_the_key_in_the_second_block() -> None:
    """An error in the second entry names that entry and the line of its key."""
    second = _ENTRY.split("\n", 3)[3].replace(
        "api/data/knowledge_embeddings.json", "api/data/other.json"
    )
    error = _refused(_ENTRY + "\n" + second.replace('"--check"]', '"a b"]'))
    assert error.key == "review.generated[1].check"
    assert error.line == 12


def test_error_without_a_literal_key_names_the_block_header() -> None:
    """A missing required key is named on its block's header line."""
    text = (
        '[project]\nlanguages = ["python"]\n\n[[review.generated]]\n'
        'path = "d/x.json"\nsources = ["x"]\n'
    )
    error = _refused(text)
    assert error.key == "review.generated[0].check"
    assert error.line == 4


def test_a_class_never_matches_a_slash() -> None:
    """A character class, plain or negated, stays inside one path segment."""
    assert glob_matches("data/v[/x].json", "data/vx.json")
    assert not glob_matches("data/v[/x].json", "data/v/.json")


@pytest.mark.parametrize("script", ["-cimport os.py", "scripts/a b.py", "scripts/$(x).sh"])
def test_check_script_that_an_interpreter_would_misread_is_refused(script: str) -> None:
    """check[0] goes through the plain-token rule and may not start with '-'."""
    error = _refused(_ENTRY.replace("scripts/index_knowledge_corpus.py", script))
    assert error.key == "review.generated[0].check"
    assert error.line == 6


@pytest.mark.parametrize(
    "glob",
    [
        "**/*.yml",
        ".github/**",
        "*.toml",
        "**/package.json",
        "*",
        ".github/workflows/auto-review.yml",
        ".gitlab/ci/review.yml",
        "ci/review-gate.sh",
        "ci/**",
        "*.lock",
        "**/*.yaml",
        "**/package-lock.json",
        "go.sum",
        "Jenkinsfile",
        "azure-pipelines.yml",
        "Dockerfile.*",
        "tox.ini",
        "scripts/*.sh",
        "tools/deploy",
        "bin/ru?",
        "web/*.tsx",
        "lib/*.php",
        "**/*.xml",
        "**/*.gradle",
        ".devcontainer/**",
        ".vscode/tasks.json",
        "api/requirements.txt",
        "frontend/package-lock.json",
        "frontend/**/*.json",
        "services/api/pyproject.toml",
        ".yarnrc.yml",
        ".cargo/config.toml",
        ".bazelrc",
        ".envrc",
        "pip.conf",
        "pytest.ini",
        "ruff.toml",
        "Taskfile.yml",
        ".coveragerc",
        "conf/app.properties",
        "web/tsconfig.json",
        "web/tsconfig.base.json",
        "composer.json",
        "renovate.json",
        "deno.json",
    ],
)
def test_a_glob_reaching_ci_or_manifests_is_refused(glob: str) -> None:
    """CI configuration, repoach.toml and dependency manifests are never declared away."""
    error = _refused(_ENTRY.replace("api/data/knowledge_embeddings.json", glob), languages=())
    assert error.key == "review.generated[0].path"
    assert error.line == 5
    assert "always reviewed" in str(error)


def test_a_double_star_slash_matches_zero_directories() -> None:
    """``a/**/b`` matches ``a/b`` as well as ``a/x/y/b``."""
    assert glob_matches("api/**/vectors.json", "api/vectors.json")
    assert glob_matches("api/**/vectors.json", "api/x/y/vectors.json")
    assert not glob_matches("api/**/vectors.json", "apix/vectors.json")


def test_an_extensionless_glob_is_refused_whatever_the_languages() -> None:
    """``data/*`` can match ``data/x.sh``: refused with Python declared and with none."""
    text = _ENTRY.replace("api/data/knowledge_embeddings.json", "data/*")
    bare = text.replace('languages = ["python"]', "languages = []")
    assert _refused(bare, languages=()).key == "review.generated[0].path"
    assert _refused(text).key == "review.generated[0].path"


@pytest.mark.parametrize(
    ("glob", "hidden"),
    [
        ("scripts/index_knowledge_corpus.py", "scripts/index_knowledge_corpus.py"),
        ("api/data/knowledge_corpus.json", "api/data/knowledge_corpus.json"),
        ("docs/user_guide/a.md", "docs/user_guide/**"),
        ("api/data/_header.json", "api/data/_header.json"),
        ("docs/**/*.npy", "docs/user_guide/**"),
    ],
)
def test_a_path_hiding_its_prover_is_refused(glob: str, hidden: str) -> None:
    """A path glob may not summarise away a check script, a source or a producer input."""
    error = _refused(
        _ENTRY.replace('path = "api/data/knowledge_embeddings.json"', f'path = "{glob}"'),
        languages=(),
    )
    assert error.key == "review.generated[0].path"
    assert error.line == 5
    assert hidden in str(error)


def test_a_path_hiding_another_entrys_script_is_refused() -> None:
    """The second entry's path may not match the first entry's check script."""
    second = (
        '\n[[review.generated]]\npath = "tools/gen"\ncheck = ["tools/other.sh"]\nsources = ["x"]\n'
    )
    first = _ENTRY.replace("scripts/index_knowledge_corpus.py", "tools/gen")
    error = _refused(first + second, languages=())
    assert error.key == "review.generated[1].path"
    assert "tools/gen" in str(error)


@settings(max_examples=300, deadline=None)
@given(
    pattern=st.text(alphabet="abé*?.", min_size=1, max_size=8),
    name=st.text(alphabet="abé.", min_size=0, max_size=8),
)
def test_a_one_segment_glob_agrees_with_fnmatch(pattern: str, name: str) -> None:
    """Inside one segment, ``*`` and ``?`` mean what fnmatch means, non-ASCII names included."""
    assert glob_matches(pattern, name) == fnmatch.fnmatchcase(name, pattern)
    assert not glob_matches(pattern, f"{name}/{name}") or "**" in pattern
