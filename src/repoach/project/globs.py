"""Repository globs and the language of the files a glob can match.

``**`` crosses directories, ``*`` and ``?`` stay inside one segment. The
``[[review.generated]]`` declarations of ``repoach.toml`` use them for
their ``path``, ``sources`` and ``producer_inputs``, and refuse a ``path``
that could match code (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ G1). The
code extensions come from the caller: this module imports nothing of
repoach.
"""

from __future__ import annotations

import re

GLOB_SPECIALS = frozenset("*?[")
"""Characters that make a path a glob."""


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a repository glob: ``**`` crosses directories, ``*`` and ``?`` do not.

    Args:
        pattern: A repository-relative glob (``docs/user_guide/**``).

    Returns:
        A regex matching the whole repository-relative path.
    """
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            parts.append(".*")
            index += 2
            continue
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        elif char == "[":
            close = pattern.find("]", index + 1)
            if close == -1:
                parts.append(re.escape(char))
            else:
                body = pattern[index + 1 : close].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                parts.append(f"(?!/)[{body}]")
                index = close
        else:
            parts.append(re.escape(char))
        index += 1
    return re.compile("".join(parts) + r"\Z")


def glob_matches(pattern: str, path: str) -> bool:
    """Return whether the repository glob ``pattern`` matches ``path`` (:func:`glob_regex`).

    Args:
        pattern: A repository-relative glob (``docs/user_guide/**``).
        path: A repository-relative path.

    Returns:
        True when the whole of ``path`` matches.
    """
    return glob_regex(pattern).match(path) is not None


def _glob_can_match_suffix(pattern: str, suffix: str) -> bool:
    """Whether ``pattern`` can match some path ending in ``suffix``.

    Every wildcard is tried, in turn, as the one that swallows ``x`` plus
    the suffix, the others standing for ``x``; a literal ending is tried
    as written.
    """
    regex = glob_regex(pattern)
    tokens = re.split(r"(\*\*|\*|\?|\[[^\]]*\])", pattern)
    wildcards = [i for i, token in enumerate(tokens) if token and token[0] in GLOB_SPECIALS]
    probes = ["".join("x" if i in wildcards else token for i, token in enumerate(tokens))]
    for chosen in wildcards:
        probes.append(
            "".join(
                ("x" + suffix if i == chosen else "x") if i in wildcards else token
                for i, token in enumerate(tokens)
            )
        )
    return any(probe.endswith(suffix) and regex.match(probe) for probe in probes)


def glob_code_extension(pattern: str, extensions: tuple[str, ...]) -> str | None:
    """Return the code extension ``pattern`` could match, or ``None``.

    A generated-artefact glob may never match a file of a declared
    language: code is read by the reviewers, never declared away.

    Args:
        pattern: A ``[[review.generated]]`` ``path`` glob.
        extensions: The file extensions of the declared languages.

    Returns:
        The first matching extension (``.py``), ``None`` when none can match.
    """
    for suffix in extensions:
        if _glob_can_match_suffix(pattern, suffix):
            return suffix
    return None


ALWAYS_REVIEWED_PROBES: tuple[str, ...] = (
    "repoach.toml",
    ".gitlab-ci.yml",
    ".gitlab/ci/jobs.yml",
    ".github/workflows/ci.yml",
    ".github/CODEOWNERS",
    "CODEOWNERS",
    ".githooks/pre-commit",
    ".gitattributes",
    ".gitmodules",
    "Dockerfile",
    "Makefile",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "noxfile.py",
    ".npmrc",
    ".pypirc",
    "Jenkinsfile",
    ".travis.yml",
    ".drone.yml",
    ".gitlab-ci.yaml",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    "Dockerfile.ci",
    "Containerfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "gradle.properties",
    "build.sbt",
    "Gemfile",
    "Rakefile",
    "Pipfile",
    "Vagrantfile",
    "flake.nix",
    "default.nix",
    "WORKSPACE",
    "BUILD.bazel",
    ".bazelrc",
    ".yarnrc",
    ".yarnrc.yml",
    ".cargo/config.toml",
    ".envrc",
    "pip.conf",
    "uv.toml",
    ".tool-versions",
    ".nvmrc",
    "MANIFEST.in",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
    ".pre-commit-config.yaml",
    "docker-compose.yml",
    "compose.yaml",
)
"""Files a ``[[review.generated]]`` ``path`` glob may never match.

What runs or installs on CI (its configuration, hooks, ``repoach.toml``,
dependency manifests and lockfiles, whose resolved sources a summary would
hide) is always read by the reviewers: a glob such as
``**/*.yml`` or ``*.toml`` that would summarise one of them away is refused."""

ALWAYS_REVIEWED_DIRS: tuple[str, ...] = (
    ".github/",
    ".gitlab/",
    ".githooks/",
    ".circleci/",
    ".buildkite/",
    ".devcontainer/",
    ".vscode/",
    ".idea/",
    "ci/",
)
"""Directories whose files a ``path`` glob may never reach, at any depth below them.

CI definitions and the scripts CI runs live there; a literal path such as
``.github/workflows/auto-review.yml`` or ``ci/review-gate.sh`` is refused
like a wildcard reaching them."""


ALWAYS_CODE_EXTENSIONS: tuple[str, ...] = (
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".bat",
    ".cmd",
    ".py",
    ".rb",
    ".pl",
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".php",
    ".lua",
    ".psm1",
    ".pyw",
    ".tcl",
)
"""Script extensions a ``path`` glob may never match, whatever languages a project declares."""


CONFIG_EXTENSIONS: tuple[str, ...] = (
    ".ini",
    ".cfg",
    ".conf",
    ".toml",
    ".yml",
    ".yaml",
    ".lock",
    ".properties",
    ".gradle",
    ".kts",
    ".xml",
    ".nix",
    ".bzl",
    ".bazel",
    ".mk",
    ".env",
)
"""Configuration formats a ``path`` glob may never match.

A tool reads its settings from such a file (``pytest.ini``, ``ruff.toml``,
``Taskfile.yml``); no list of names can be complete, so the format itself
is refused. A project deriving data in one of them keeps it reviewed."""


def always_reviewed(pattern: str) -> str | None:
    """Return the always-reviewed file or directory ``pattern`` can reach, or ``None``.

    Args:
        pattern: A ``[[review.generated]]`` ``path`` glob.

    A probe file is looked for at the root and in any directory, so
    ``api/requirements.txt`` and ``frontend/**/*.json`` (which can match
    ``frontend/package-lock.json``) are caught like the root files.

    Returns:
        The probe file or directory prefix it reaches, ``None`` when it
        reaches none.
    """
    literal = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    for directory in ALWAYS_REVIEWED_DIRS:
        if literal.startswith(directory) or glob_matches(pattern, directory + "x/y.yml"):
            return directory
        if glob_matches(pattern, directory + "x.sh") or glob_matches(pattern, directory + "x"):
            return directory
    head = literal.rsplit("/", 1)[0] + "/" if "/" in literal else ""
    for probe in ALWAYS_REVIEWED_PROBES:
        name = probe.rsplit("/", 1)[-1]
        placed = (probe, head + name, head + "x/" + name, "x/" + name, head + probe)
        if any(glob_matches(pattern, candidate) for candidate in placed):
            return probe
    script = glob_code_extension(pattern, ALWAYS_CODE_EXTENSIONS)
    if script is not None:
        return f"a {script} script"
    config = glob_code_extension(pattern, CONFIG_EXTENSIONS)
    if config is not None:
        return f"a {config} configuration file"
    if any(part.startswith(".") for part in literal.split("/")):
        return "a dotfile or a file under a dot directory, which tools read as configuration"
    last = pattern.rsplit("/", 1)[-1]
    if "." not in last:
        return "an extensionless file, which may be an executable"
    return None


__all__ = [
    "ALWAYS_CODE_EXTENSIONS",
    "ALWAYS_REVIEWED_DIRS",
    "ALWAYS_REVIEWED_PROBES",
    "CONFIG_EXTENSIONS",
    "GLOB_SPECIALS",
    "always_reviewed",
    "glob_code_extension",
    "glob_matches",
    "glob_regex",
]
