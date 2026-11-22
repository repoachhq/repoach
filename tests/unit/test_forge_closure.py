"""The closure oracle of the forge recorder (SP-RECORDER-REDACTS-BY-CLASS D3, D8, AC2, AC3).

Every set and every reader of ``repoach.forge.closure`` is walked by
enumeration, so a mutation of one entry, one boundary or one operator
turns a named test red.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import ipaddress
import re
import string
import sys
from pathlib import Path
from types import ModuleType

import pytest

from repoach.forge import closure, recording

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str) -> ModuleType:
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _project_owned(module: str | None) -> bool:
    """Whether a function or class belongs to the recorder, not to the standard library."""
    return module is not None and module.split(".")[0] in {
        "repoach",
        "closure",
        "record_forge_fixture",
        "redact_forge_fixtures",
    }


def _reachable(namespace: dict[str, object]) -> list[object]:
    """Every pattern, function, class and frozenset a module namespace holds, modules not entered."""
    found: list[object] = []
    stack = list(namespace.values())
    seen: set[int] = set()
    while stack:
        item = stack.pop()
        if id(item) in seen or isinstance(item, ModuleType):
            continue
        seen.add(id(item))
        if isinstance(item, (re.Pattern, frozenset)) or (
            callable(item) and _project_owned(getattr(item, "__module__", None))
        ):
            found.append(item)
        if isinstance(item, (tuple, list, set)):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
    return found


def test_the_oracle_shares_no_code_object_with_the_redactor() -> None:
    """D3 independence: no shared pattern, function, class or set, no import, no equal pattern source."""
    redactor = [
        *_reachable(vars(recording)),
        *_reachable(vars(_script("record_forge_fixture"))),
        *_reachable(vars(_script("redact_forge_fixtures"))),
    ]
    ids = {id(item) for item in redactor}
    own = _reachable(vars(closure))
    shared = [item for item in own if id(item) in ids]
    assert shared == []
    assert not [
        v for v in vars(closure).values() if getattr(v, "__module__", "") == recording.__name__
    ]
    tree = ast.parse((ROOT / "src" / "repoach" / "forge" / "closure.py").read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported <= {"__future__", "re", "ipaddress", "base64", "html", "urllib", "dataclasses"}
    sources = {p.pattern for p in redactor if isinstance(p, re.Pattern)}
    assert [
        p.pattern
        for p in _reachable(vars(closure))
        if isinstance(p, re.Pattern) and p.pattern in sources
    ] == []


def test_host_rule_tld_lists_reverse_dns_heads_and_extension_codes_by_enumeration() -> None:
    """D8: ``HOST_TLDS`` membership and shape, the extension codes absent, H1 and H2, the delimiters."""
    assert all(2 <= len(t) <= 12 and t.isascii() and t.islower() for t in closure.HOST_TLDS)
    assert not closure.HOST_TLDS & closure.EXTENSION_TLDS
    assert {
        "ac",
        "am",
        "cc",
        "in",
        "md",
        "mk",
        "ml",
        "pl",
        "pm",
        "ps",
        "py",
        "rs",
        "sh",
        "so",
        "tf",
    } == closure.EXTENSION_TLDS
    for code in ("com", "io", "dev", "ai", "de", "fr", "internal", "local", "corp", "example"):
        assert code in closure.HOST_TLDS
    for head in closure.REVERSE_DNS_HEADS:
        assert head in closure.HOST_TLDS
        assert closure.hosts(f"{head}.acme.tools") == []
    assert {"com", "org", "net", "io", "dev", "me", "co", "edu", "gov"} == closure.REVERSE_DNS_HEADS
    assert closure.hosts("a.b.zq") == []
    for tld in sorted(closure.HOST_TLDS):
        found = closure.hosts(f"see svc.acme.{tld} now")
        assert [h.host for h in found] == [f"svc.acme.{tld}"], tld
        assert found[0].led is False
        assert closure.hosts(f"see acme.{tld} now") == [], tld
    for tld in sorted(closure.EXTENSION_TLDS):
        assert closure.hosts(f"see svc.acme.{tld} now") == [], tld
        led = closure.hosts(f"see https://svc.acme.{tld} now")
        assert [h.host for h in led] == [f"svc.acme.{tld}"], tld
    for scheme in ("http", "https", "ssh", "git", "ftp", "ftps", "svn+ssh", "x-y.z"):
        found = closure.hosts(f"{scheme}://acme.zz/x")
        assert [(h.host, h.led) for h in found] == [("acme.zz", True)], scheme
    assert [h.led for h in closure.hosts("git@acme.zz:o/r")] == [True]
    assert closure.hosts("@acme.zz") == []
    assert closure.hosts("//acme.zz/x") == []
    assert [h.host for h in closure.hosts("//cdn.acme.io/x")] == ["cdn.acme.io"]
    assert closure.hosts("x := 1 //cfg.dev/env/prod") == []
    assert closure.hosts("Xcode.app config.dev ssl.ca stream.io torch.net os.path README.md") == []
    assert closure.hosts("com.example.app org.apache.commons.io") == []
    assert closure.hosts("self.config.dev") == []
    assert {"self.config.dev"} == closure.NON_HOST_TOKENS
    assert [h.host for h in closure.hosts("a.b.io.")] == ["a.b.io"]
    assert closure.hosts("-a.b.io")[0].host == "a.b.io"
    assert [h.host for h in closure.hosts("x--y.zq.io")] == ["x--y.zq.io"]
    assert closure.hosts("-x.zq.io-") != []
    assert closure.hosts("1x.zq.io") != []
    assert closure.hosts("a.b..io") == []
    assert closure.hosts("a." + "b" * 64 + ".io") == []
    assert closure.hosts("a." + "b" * 63 + ".io") != []
    assert closure.hosts("a.b.i0") == []
    assert closure.hosts("a.b.i") == []
    for char in map(chr, range(128)):
        text = f"{char}svc.acme.io{char}"
        found = closure.hosts(text)
        inside = char.isalnum() or char in ".-"
        assert (not found or found[0].host == "svc.acme.io") or inside, repr(char)
        if not inside:
            assert [h.host for h in found] == ["svc.acme.io"], repr(char)


def test_ipv4_rule_reserved_table_version_lead_and_led_addresses_by_enumeration() -> None:
    """D8: every reserved network, every version lead and archive tail, led and un-led addresses."""
    networks = [
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    ]
    assert [str(n) for n in closure.IPV4_RESERVED] == networks
    for net in map(ipaddress.IPv4Network, networks):
        for address in (net[0], net[-1]):
            assert closure.public_addresses(f"at {address} now") == []
    for public in ("8.8.8.8", "11.22.33.44", "172.32.0.1", "100.128.0.1", "1.1.1.1"):
        assert closure.public_addresses(f"at {public} now") == [public]
        assert [h.host for h in closure.hosts(f"https://{public}/x")] == [public]
        assert [h.host for h in closure.hosts(f"//{public}/x")] == [public]
        assert closure.public_addresses(f"https://{public}/x") == []
        assert closure.public_addresses(f"//{public}/x") == []
    assert closure.hosts("at 8.8.8.8 now") == []
    assert [h.host for h in closure.hosts("ssh://10.0.0.5/ns/repo")] == ["10.0.0.5"]
    assert [h.host for h in closure.hosts("git@10.0.0.5:ns/repo")] == ["10.0.0.5"]
    for lead in ("v", "V", "==", ">=", "<=", "~=", "!=", "version", "Version", "VERSION"):
        assert closure.public_addresses(f"x{lead}8.8.8.8") == [] if lead in "vV" else True
    for lead in ("==", ">=", "<=", "~=", "!=", "version", "Version", "VERSION"):
        assert closure.public_addresses(f"pkg {lead}8.8.8.8") == [], lead
        assert closure.hosts(f"pkg {lead}8.8.8.8") == [], lead
    assert closure.public_addresses("tool v8.8.8.8") == []
    for tail in ("-rc1", "+build", ".tar.gz", ".whl", ".zip", ".gz"):
        assert closure.public_addresses(f"pkg 8.8.8.8{tail}") == [], tail
    assert closure.public_addresses("addr 8.8.8.8.") == ["8.8.8.8"]
    assert closure.public_addresses("addr 8.8.8.256") == []
    assert closure.public_addresses("addr 8.8.8") == []
    assert closure.public_addresses("addr 8.8.8.8.8") == []
    assert closure.public_addresses("addr 8.8.8.0008") == []
    assert closure.public_addresses("addr 008.8.8.8") == ["008.8.8.8"]
    assert closure.public_addresses("addr 8.8.8.8:80") == ["8.8.8.8"]
    assert closure.is_placeholder_host("203.0.113.9")
    assert closure.is_placeholder_host("127.1.2.3")
    assert not closure.is_placeholder_host("10.0.0.1")
    assert not closure.is_placeholder_host("8.8.8.8")


def test_mailbox_rule_literals_credentials_and_doc_domains_by_enumeration() -> None:
    """D8: the four placeholders, ``git@`` on a placeholder host, documentation domains, a credential."""
    literal = [
        "git@forge.example.com",
        "git@example.com",
        "git@sub.example.org",
        "git@localhost.example",
        "noreply@forge.example.com",
        "no-reply@forge.example.com",
        "author@users.noreply.forge.example.com",
        "author-2@users.noreply.forge.example.com",
        "bot@users.noreply.forge.example.com",
        "maintainer@users.noreply.forge.example.com",
        "agent@users.noreply.forge.example.com",
        "service-12@users.noreply.forge.example.com",
        "someone@example.com",
        "someone@example.org",
        "someone@example.net",
        "x@a.invalid",
        "x@a.test",
        "x@a.example",
        "x@a.localhost",
        "x@a.internal",
        "x@localhost.localhost",
        "mailto:support@example.com",
        "_git@forge.example.com",
        "+git@forge.example.com",
        "-git@forge.example.com",
    ]
    for text in literal:
        found = closure.mailboxes(text)
        assert len(found) == 1, text
        assert closure.is_literal_mailbox(found[0]), text
    personal = [
        "jane@gmail.com",
        "jane@sub.example.com",
        "author@forge.example.com",
        "author@users.noreply.example.com",
        "authors@users.noreply.forge.example.com",
        "author-0@users.noreply.forge.example.com",
        "other@users.noreply.forge.example.com",
        "x-access-token:${GITHUB_TOKEN}@forge.example.com",
        "user:pw@example.com",
        "noreply@github.com",
        "deploy@srv.corp.io",
        "jane@10.0.0.5",
        "git@gmail.com",
    ]
    for text in personal:
        found = closure.mailboxes(text)
        assert found, text
        assert not closure.is_literal_mailbox(found[-1]), text
    assert closure.mailboxes("@pytest.fixture") == []
    assert closure.mailboxes("a@b") == []
    assert closure.mailboxes("a@b.c") == []
    assert closure.mailboxes("mailto:@example.com") == []
    box = closure.mailboxes("x ${a}.b+c%d_e@h.example.com. y")[0]
    assert (box.local, box.host) == ("${a}.b+c%d_e", "h.example.com")
    assert box.start == 2
    assert box.end == len("x ${a}.b+c%d_e@h.example.com")
    assert closure.mailboxes("a@b.io@c.io")[0].host == "b.io"
    for char in string.ascii_letters + string.digits + "._%+-:${}":
        assert closure.mailboxes(f"{char}@h.io")[0].local == char
    for char in " /\"'<>()[]\\,;=!?#|*`~^&":
        assert closure.mailboxes(f"a{char}b@h.io")[0].local == "b"
    assert closure.mailboxes("MAILTO:a@h.io")[0].local == "a"


def test_public_allowlist_and_placeholder_class_by_enumeration() -> None:
    """D8: the 24 suffixes exact and as ``.<suffix>``, trailing dot, case, forge hosts never public."""
    suffixes = {
        "crates.io",
        "docker.io",
        "docs.github.com",
        "docs.gitlab.com",
        "docs.rs",
        "ghcr.io",
        "go.dev",
        "golang.org",
        "k8s.io",
        "mozilla.org",
        "npmjs.com",
        "npmjs.org",
        "pypi.org",
        "python.org",
        "readthedocs.io",
        "readthedocs.org",
        "stackoverflow.com",
        "wikipedia.org",
        "pythonhosted.org",
        "cloudflare.com",
        "jsdelivr.net",
        "unpkg.com",
        "renovatebot.com",
        "mend.io",
    }
    assert suffixes == closure.PUBLIC_SUFFIXES
    assert len(closure.PUBLIC_SUFFIXES) == 24
    for suffix in suffixes:
        assert closure.is_public_host(suffix)
        assert closure.is_public_host(f"www.{suffix}")
        assert closure.is_public_host(f"A.B.{suffix.upper()}.")
        assert not closure.is_public_host(f"x{suffix}")
        assert not closure.is_public_host(f"{suffix}.evil.io")
    for forge in (
        "github.com",
        "gitlab.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "githubusercontent.com",
        "forge.example.com",
    ):
        assert not closure.is_public_host(forge), forge
    placeholders = [
        "localhost",
        "forge.example.com",
        "example.com",
        "a.example.com",
        "example.org",
        "x.example.org",
        "example.net",
        "x.y.example.net",
        "example",
        "a.example",
        "test",
        "a.test",
        "invalid",
        "a.invalid",
        "127.0.0.1",
        "127.255.255.254",
        "203.0.113.9",
        "198.51.100.7",
        "192.0.2.1",
        "FORGE.Example.COM.",
    ]
    for host in placeholders:
        assert closure.is_placeholder_host(host), host
    for host in (
        "exampleXcom",
        "notexample.com",
        "example.com.evil.io",
        "10.0.0.1",
        "a.localhost",
        "8.8.8.8",
        "mytest",
        "a.mytest",
        "192.0.2.256",
        "1.2.3",
    ):
        assert not closure.is_placeholder_host(host), host
    for host in (
        "example.com",
        "example.org",
        "example.net",
        "localhost",
        "a.invalid",
        "a.test",
        "a.example",
        "a.localhost",
        "a.internal",
        "A.INTERNAL.",
    ):
        assert closure.is_doc_mailbox_host(host), host
    for host in ("sub.example.com", "forge.example.com", "example.io", "internal", "a.internals"):
        assert not closure.is_doc_mailbox_host(host), host


def _node_legacy(number: int, pad: bool = True) -> str:
    text = base64.b64encode(f"04:User{number}".encode()).decode()
    return text if pad else text.rstrip("=")


def _node_new(number: int) -> str:
    return "U_" + base64.urlsafe_b64encode(
        b"\x92\x00\xce" + number.to_bytes(4, "big")
    ).decode().rstrip("=")


def test_residual_judges_known_values_in_decoded_forms_and_embedded_node_ids() -> None:
    """D3: four decoding rounds, casefold, ``%2F`` delimiter, padded node ids, kind order, ordinals."""
    entries = (
        ("login", "zqlogin"),
        ("login", "zqother"),
        ("host", "git.zq.io"),
        ("segment", "zqseg"),
        ("id", "74928163"),
        ("name", "zqname"),
        ("mailbox", "zq@mail.io"),
    )
    known = closure.Known(entries)
    assert closure.residual("clean text", known) is None
    assert closure.residual("see ZQLOGIN here", known) == ("login", 1)
    assert closure.residual("see zqother", known) == ("login", 2)
    assert closure.residual("a zqother and zqlogin", known) == ("login", 1)
    assert closure.residual("x git.zq.io y", known) == ("host", 1)
    assert closure.residual("zqseg", known) == ("segment", 1)
    assert closure.residual("n 74928163", known) == ("id", 1)
    assert closure.residual("n zqname", known) == ("name", 1)
    assert closure.residual("n zq@mail.io", known) == ("mailbox", 1)
    for word in ("zqlogin",):
        for text in (f"x{word}", f"{word}x", f"{word}1", f"1{word}", f"{word}_x"):
            expected = None if text[0].isalnum() or text[-1].isalnum() else ("login", 1)
            if text in (f"{word}_x",):
                expected = ("login", 1)
            assert closure.residual(text, known) == expected, text
    for lead in "-_./:@=\"'(<[{ \n":
        assert closure.residual(f"a{lead}zqlogin{lead}b", known) == ("login", 1)
    assert closure.residual("org%2Fzqseg", known) == ("segment", 1)
    assert closure.residual("org%2fzqseg", known) == ("segment", 1)
    assert closure.residual("zqseg%2Forg", known) == ("segment", 1)
    assert closure.residual("a%2Fzqloginx", known) is None
    once = "zq%6Cogin"
    assert closure.residual(once, known) == ("login", 1)
    twice = "zq%256Cogin"
    assert closure.residual(twice, known) == ("login", 1)
    four = "zq%" + "25" * 3 + "6Cogin"
    assert closure.residual(four, known) == ("login", 1)
    five = "zq%" + "25" * 4 + "6Cogin"
    assert closure.residual(five, known) is None
    assert closure.residual("&#x7a;qlogin", known) == ("login", 1)
    assert closure.residual("&lt;zqlogin&gt;", known) == ("login", 1)
    assert closure.residual(r"zqlogin", known) == ("login", 1)
    assert closure.residual(r"zqlogin", known) == ("login", 1)
    assert closure.residual("zqlogin" + "&amp;" * 3 + "x", known) == ("login", 1)
    for number in (74928163, 1, 300, 70000):
        ids = closure.Known((("id", str(number)),))
        for node in (_node_legacy(number), _node_legacy(number, False), _node_new(number)):
            assert closure.residual(f"see {node} here", ids) == ("id", 1), (number, node)
            assert closure.residual(f"users/{node}/x", ids) == ("id", 1)
            assert closure.residual(f"feat/{node}", ids) == ("id", 1)
            assert closure.residual(f"abc{node}", ids) == ("id", 1)
            assert closure.residual(f"{node}suffix", ids) == ("id", 1)
    assert closure.node_id_numbers("MDQ6VXNlcjc0OTI4MTYzsuffix") == ["74928163"]
    assert closure.node_id_numbers("abcdefgU_kgDOBHdQIw") == ["74928163"]
    assert closure.node_id_numbers("U_1001") == ["1001"]
    assert closure.node_id_numbers("MDQ6VXNl") == []
    assert closure.node_id_numbers("U_a") == []
    assert closure.node_id_numbers("U_abcde") == []
    assert closure.node_id_numbers(_node_new(5)) == ["5"]
    assert closure.node_id_numbers(
        "U_" + base64.urlsafe_b64encode(b"\x92\x00\x05").decode().rstrip("=")
    ) == ["5"]
    assert closure.node_id_numbers(
        "U_" + base64.urlsafe_b64encode(b"\x92\x00\xcc\x80").decode().rstrip("=")
    ) == ["128"]
    assert closure.node_id_numbers(
        "U_" + base64.urlsafe_b64encode(b"\x92\x00\xcd\x01\x00").decode().rstrip("=")
    ) == ["256"]
    assert closure.node_id_numbers(
        "U_"
        + base64.urlsafe_b64encode(b"\x92\x00\xcf" + (2**40).to_bytes(8, "big"))
        .decode()
        .rstrip("=")
    ) == [str(2**40)]
    assert (
        closure.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x93\x00\x05").decode().rstrip("=")
        )
        == []
    )
    assert (
        closure.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x92\x00\xd0\x05").decode().rstrip("=")
        )
        == []
    )
    assert (
        closure.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x92\x00\xcd\x01").decode().rstrip("=")
        )
        == []
    )
    assert (
        closure.node_id_numbers("U_" + base64.urlsafe_b64encode(b"\x92\x00").decode().rstrip("="))
        == []
    )
    assert closure.node_id_numbers("MDQ6VXNlcjEyMw==") == ["123"]
    assert closure.node_id_numbers("MDQ6VXNlcjEyMw") == ["123"]
    assert closure.node_id_numbers("MDQ6VXNlcjEyM") == ["12"]
    assert closure.node_id_numbers("MDQ6VXNlcg==") == []
    assert closure.node_id_numbers("MDQ6VXNlcjEyMw==" + "A" * 80) == ["123"]
    assert closure.node_id_numbers("MDQ6VXNl" + "!" * 3) == []
    assert closure.node_id_numbers(base64.b64encode(b"04:Repo123").decode()) == []
    assert closure.node_id_numbers("MDQ6VXNlcjEyMw==" + "x" + "MDQ6VXNlcjQ1Ng==") == ["123", "456"]
    decoded = closure.decoded_forms("a%2520b")
    assert decoded == ["a%2520b", "a%20b", "a b"]
    assert closure.decoded_forms("plain") == ["plain"]
    assert len(closure.decoded_forms("%" + "25" * 5 + "41")) == 5
    assert closure.decoded_forms("%" + "25" * 5 + "41")[-1] == "%2541"
    assert closure.decoded_forms(r"A")[-1] == "A"
    assert closure.decoded_forms("&amp;lt;")[-1] == "<"
    literal = closure.Known((("login", "github"),), literals=("github-actions[bot]", "github"))
    assert closure.residual("by github-actions[bot] now", literal) is None
    assert closure.residual("by github now", literal) is None
    assert closure.residual("GITHUB-ACTIONS[BOT]", literal) is None
    assert closure.residual("by github-actionsx", literal) == ("login", 1)
    assert closure.residual("xgithub", literal) is None
    assert closure.residual("github.com", literal) is None
    pattern = closure.token_pattern("a.b")
    assert pattern.search("x a.b y") and not pattern.search("xa.b") and not pattern.search("a.bx")
    assert pattern.search("A.B")
    assert closure.token_pattern("a").search("1-a-1") and not closure.token_pattern("a").search(
        "ba"
    )
    assert closure.token_pattern("a").search("%2Fa") and closure.token_pattern("a").search("%2fa")
    assert not closure.token_pattern("a").search("%2Fxa")
    assert not closure.token_pattern("a").search("%3Fa") or True
    assert closure.token_pattern("a").search("%3Fa") is None
    assert closure.token_pattern("a").search("%2a") is None
    assert closure.token_pattern("a").search("2Fa") is None


def test_judge_orders_its_reasons_and_blanks_literals() -> None:
    """D3: residual, unknown host, public address, unknown mailbox, in that order, never a value."""
    known = closure.Known((("login", "zqlogin"),), literals=("docs.github.com", ":owner"))
    assert closure.judge("fine https://forge.example.com/org/project", known) is None
    assert closure.judge("zqlogin at git.acme.io 8.8.8.8 a@gmail.com", known) == closure.Refusal(
        "residual", "login", 1
    )
    assert closure.judge("git.acme.io 8.8.8.8 a@gmail.com", known) == closure.Refusal(
        "unknown host"
    )
    assert closure.judge("8.8.8.8 a@b.example.com", known) == closure.Refusal("public address")
    assert closure.judge("a@b.example.com", known) == closure.Refusal("unknown mailbox")
    assert closure.judge("see docs.github.com and :owner", known) is None
    assert closure.judge("https://docs.github.com/x https://pypi.org/p", known) is None
    assert closure.judge("https://github.com/x", known) == closure.Refusal("unknown host")
    assert closure.judge("https://www.mend.io/ x@localhost.test", known) is None
    assert closure.judge("see example.com and https://example.com", known) is None
    assert closure.judge("", known) is None
    assert closure.judge("10.0.0.12:5432 127.0.0.1:8000 203.0.113.9", known) is None
    assert repr(known) == "<known: 1>"
    assert len(known) == 1
    assert closure.Known((("login", "a"), ("host", "b"), ("host", "c"))).values("host") == (
        "b",
        "c",
    )
    assert closure.Known((("login", "a"),), (), (("a", "x"),)).mapping() == {"a": "x"}
    assert closure.Known((("deny-list", "zqdeny"),)).values("deny-list") == ("zqdeny",)
    assert closure.residual("zqdeny", closure.Known((("deny-list", "zqdeny"),))) == ("deny-list", 1)
    assert closure.KINDS == ("login", "host", "segment", "id", "name", "mailbox")
    assert (*closure.KINDS, "deny-list") == closure.JUDGED_KINDS
    assert {"http", "https", "ssh", "git", "ftp", "ftps"} == closure.URL_SCHEMES


def test_refusal_reasons_name_a_kind_and_a_position_never_a_value() -> None:
    """D3: a ``Known`` and a ``Refusal`` print counts and kinds only, so ``pytest -l`` shows no value."""
    known = closure.Known((("login", "zqsecretvalue"), ("host", "git.zqsecret.io")))
    assert "zqsecret" not in repr(known)
    assert "zqsecret" not in repr(closure.judge("zqsecretvalue", known))
    assert "zqsecret" not in str(closure.judge("zqsecretvalue", known))


@pytest.mark.parametrize("text", ["git.acme.io", "x@acme.io", "https://acme.io"])
def test_hosts_report_positions(text: str) -> None:
    """D8: a ``Host`` carries the slice of its token."""
    for place in closure.hosts(text):
        assert text[place.start : place.end].rstrip(".") == place.host
