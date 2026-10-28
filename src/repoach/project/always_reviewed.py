"""What a ``[[review.generated]]`` ``path`` may never reach: the files reviewers must read.

CI and IDE definitions, the scripts and configuration tools run, build
and dependency manifests and lockfiles, ``repoach.toml``: summarising one
of them away would hide code or settings that run on CI
(SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ G1). The matching itself is
:mod:`repoach.project.globs`.
"""

from __future__ import annotations

import re

from .globs import GLOB_SPECIALS, glob_code_extension, glob_matches, glob_regex

CI_FILES: tuple[str, ...] = (
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    ".gitlab/ci/jobs.yml",
    ".github/workflows/ci.yml",
    ".github/CODEOWNERS",
    "CODEOWNERS",
    ".githooks/pre-commit",
    ".pre-commit-config.yaml",
    "Jenkinsfile",
    ".travis.yml",
    ".drone.yml",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
)
"""Continuous-integration definitions and repository hooks."""

BUILD_FILES: tuple[str, ...] = (
    "Makefile",
    "Dockerfile",
    "Dockerfile.ci",
    "Containerfile",
    "docker-compose.yml",
    "compose.yaml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "gradle.properties",
    "build.sbt",
    "Rakefile",
    "Vagrantfile",
    "flake.nix",
    "default.nix",
    "WORKSPACE",
    "BUILD.bazel",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "noxfile.py",
    "MANIFEST.in",
)
"""Build, container and task definitions."""

DEPENDENCY_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "Pipfile.lock",
    "uv.lock",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "composer.json",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "Gemfile.lock",
)
"""Dependency manifests and lockfiles, whose resolved sources a summary would hide."""

TOOL_FILES: tuple[str, ...] = (
    "repoach.toml",
    ".gitattributes",
    ".gitmodules",
    ".npmrc",
    ".pypirc",
    "pip.conf",
    "uv.toml",
    ".bazelrc",
    ".yarnrc",
    ".yarnrc.yml",
    ".cargo/config.toml",
    ".envrc",
    ".tool-versions",
    ".nvmrc",
    "renovate.json",
    "deno.json",
    "deno.jsonc",
    "biome.json",
    "lerna.json",
    "nx.json",
    "turbo.json",
    "vercel.json",
    "firebase.json",
    "angular.json",
    "project.json",
)
"""Settings tools and package managers read, ``repoach.toml`` included."""

ALWAYS_REVIEWED_PROBES: tuple[str, ...] = CI_FILES + BUILD_FILES + DEPENDENCY_FILES + TOOL_FILES
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
    ".mvn/",
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


TOOL_CONFIG_STEMS: tuple[str, ...] = (
    "tsconfig",
    "jsconfig",
    "babel.config",
    "jest.config",
    "eslint.config",
    "vite.config",
    "vitest.config",
    "webpack.config",
    "rollup.config",
    "tailwind.config",
    "postcss.config",
    "next.config",
    "nuxt.config",
    "playwright.config",
    "cypress.config",
)
"""Name stems of tool configuration files, whatever their extension or suffix (``tsconfig.base.json``)."""


def _wildcard_dir_can_be_dotted(pattern: str) -> bool:
    """Whether a wildcard standing for a directory could name a dot directory (``*/settings.json``)."""
    regex = glob_regex(pattern)
    segments = pattern.split("/")
    for index, segment in enumerate(segments[:-1]):
        if any(char in segment for char in GLOB_SPECIALS):
            probe = [
                ".x" if i == index else re.sub(r"[*?]|\[[^\]]*\]", "x", part)
                for i, part in enumerate(segments)
            ]
            if regex.match("/".join(probe)):
                return True
    return False


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
    last_literal = literal.rsplit("/", 1)[-1]
    for stem in TOOL_CONFIG_STEMS:
        if last_literal.startswith(stem) or glob_matches(pattern, head + stem + ".json"):
            return f"a {stem} configuration file"
    script = glob_code_extension(pattern, ALWAYS_CODE_EXTENSIONS)
    if script is not None:
        return f"a {script} script"
    config = glob_code_extension(pattern, CONFIG_EXTENSIONS)
    if config is not None:
        return f"a {config} configuration file"
    if any(part.startswith(".") for part in literal.split("/")) or _wildcard_dir_can_be_dotted(
        pattern
    ):
        return "a dotfile or a file under a dot directory, which tools read as configuration"
    last = pattern.rsplit("/", 1)[-1]
    if "." not in last:
        return "an extensionless file, which may be an executable"
    return None


__all__ = [
    "ALWAYS_CODE_EXTENSIONS",
    "ALWAYS_REVIEWED_DIRS",
    "ALWAYS_REVIEWED_PROBES",
    "BUILD_FILES",
    "CI_FILES",
    "CONFIG_EXTENSIONS",
    "DEPENDENCY_FILES",
    "TOOL_CONFIG_STEMS",
    "TOOL_FILES",
    "always_reviewed",
]
