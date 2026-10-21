"""``scripts/check_links.py``: links of Markdown files, network replaced by a fake transport.

SP-COMPARISON-PAGE G4 and SP-README-PRODUCT-STATEMENT G8.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_links.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_links", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_links"] = module
    spec.loader.exec_module(module)
    return module


cl = _load()


class FakeTransport:
    """A transport answering from a table, recording each call."""

    def __init__(self, table: dict[str, object]) -> None:
        self.table = table
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, user_agent: str) -> object:
        self.calls.append((url, user_agent))
        answer = self.table[url]
        if callable(answer):
            return answer(user_agent)
        return answer


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_all_links_resolve(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "LICENSE", "MIT\n")
    _write(tmp_path, "docs/guide.md", "# Guide\n\n## Run it in CI\n")
    readme = _write(
        tmp_path,
        "README.md",
        "# Top\n\n[l](LICENSE) [g](docs/guide.md#run-it-in-ci) [t](#top)\n"
        '<img src="docs/guide.md"> [w](https://ok.example/a)\n',
    )
    fake = FakeTransport({"https://ok.example/a": cl.Probe(status=200)})

    code = cl.main([str(readme)], fetch=fake)

    out = capsys.readouterr().out.strip().splitlines()
    assert code == 0
    assert out[-1] == "links: 5 checked, 0 dead"


def test_dead_links_are_named_with_file_line_and_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = _write(
        tmp_path,
        "README.md",
        "[a](missing.md)\n[b](#nowhere)\n[c](https://gone.example/x)\n[d](https://ok.example/)\n",
    )
    fake = FakeTransport(
        {
            "https://gone.example/x": cl.Probe(status=404),
            "https://ok.example/": cl.Probe(status=200),
        }
    )

    code = cl.main([str(readme)], fetch=fake)

    out = capsys.readouterr().out.strip().splitlines()
    assert code == 1
    assert f"{readme}:1 missing.md missing" in out
    assert f"{readme}:2 #nowhere no-anchor" in out
    assert f"{readme}:3 https://gone.example/x 404" in out
    assert out[-1] == "links: 4 checked, 3 dead"


def test_a_403_is_retried_once_with_a_browser_agent(tmp_path: Path) -> None:
    readme = _write(tmp_path, "README.md", "[p](https://picky.example/)\n")
    fake = FakeTransport(
        {
            "https://picky.example/": lambda ua: cl.Probe(
                status=200 if ua == cl.BROWSER_USER_AGENT else 403
            )
        }
    )

    assert cl.main([str(readme)], fetch=fake) == 0
    assert [ua for _, ua in fake.calls] == [cl.DEFAULT_USER_AGENT, cl.BROWSER_USER_AGENT]


def test_a_403_that_survives_the_retry_is_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = _write(tmp_path, "README.md", "[p](https://wall.example/)\n[o](https://ok.example/)\n")
    fake = FakeTransport(
        {"https://wall.example/": cl.Probe(status=403), "https://ok.example/": cl.Probe(status=200)}
    )

    assert cl.main([str(readme)], fetch=fake) == 1
    assert f"{readme}:1 https://wall.example/ blocked" in capsys.readouterr().out


def test_an_unresolved_host_is_dead_when_the_network_answers(tmp_path: Path) -> None:
    readme = _write(
        tmp_path, "README.md", "[x](https://example.invalid/x)\n[o](https://ok.example/)\n"
    )
    fake = FakeTransport(
        {
            "https://example.invalid/x": cl.Probe(status=None, resolved=False),
            "https://ok.example/": cl.Probe(status=200),
        }
    )

    assert cl.main([str(readme)], fetch=fake) == 1


def test_no_http_answer_at_all_is_cannot_say(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readme = _write(tmp_path, "README.md", "[a](https://a.example/)\n[b](https://b.example/)\n")
    fake = FakeTransport(
        {
            "https://a.example/": cl.Probe(status=None, resolved=False),
            "https://b.example/": cl.Probe(status=None, resolved=False),
        }
    )

    assert cl.main([str(readme)], fetch=fake) == 2
    assert "cannot say" in capsys.readouterr().out


def test_code_is_not_a_link(tmp_path: Path) -> None:
    readme = _write(
        tmp_path,
        "README.md",
        "```\n[a](https://fence.example/)\n```\n`[b](https://span.example/)` [c](mailto:x@y.z)\n",
    )

    assert cl.extract_links(readme) == []


def test_a_url_named_twice_is_fetched_once(tmp_path: Path) -> None:
    readme = _write(tmp_path, "README.md", "[a](https://ok.example/) [b](https://ok.example/)\n")
    fake = FakeTransport({"https://ok.example/": cl.Probe(status=200)})

    assert cl.main([str(readme)], fetch=fake) == 0
    assert len(fake.calls) == 1


def test_github_slugs() -> None:
    assert cl.github_slug("Works after your agents, not instead of them") == (
        "works-after-your-agents-not-instead-of-them"
    )
    assert cl.github_slug("Run `repoach` in CI") == "run-repoach-in-ci"
    assert cl.github_slug("Later: the spec-driven builder") == "later-the-spec-driven-builder"
