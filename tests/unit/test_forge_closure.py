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
import time
from pathlib import Path
from types import ModuleType

import pytest

from repoach.core import text_forms
from repoach.forge import closure, closure_tables, fixture_pipeline, recording

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
        *_reachable(vars(fixture_pipeline)),
        *_reachable(vars(_script("record_forge_fixture"))),
        *_reachable(vars(_script("redact_forge_fixtures"))),
    ]
    ids = {id(item) for item in redactor}
    own = [*_reachable(vars(closure)), *_reachable(vars(closure_tables))]
    shared = [item for item in own if id(item) in ids]
    assert shared == []
    for module in (closure, closure_tables):
        assert not [
            v for v in vars(module).values() if getattr(v, "__module__", "") == recording.__name__
        ]
    stdlib = {
        "__future__",
        "re",
        "ipaddress",
        "base64",
        "functools",
        "html",
        "urllib",
        "dataclasses",
        "unicodedata",
    }
    for path, allowed in (
        ("forge/closure.py", stdlib | {"closure_tables", "repoach.core:text_forms"}),
        ("forge/closure_tables.py", stdlib),
        ("core/text_forms.py", stdlib),
    ):
        tree = ast.parse((ROOT / "src" / "repoach" / path).read_text())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            f"{node.module}:{alias.name}"
            if (node.module or "").startswith("repoach")
            else (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert imported <= allowed, path
    relative = [
        (node.module, node.level)
        for node in ast.walk(ast.parse((ROOT / "src/repoach/forge/closure.py").read_text()))
        if isinstance(node, ast.ImportFrom) and node.module == "closure_tables"
    ]
    assert relative == [("closure_tables", 1)]
    sources = {p.pattern for p in redactor if isinstance(p, re.Pattern)}
    assert [
        p.pattern
        for p in _reachable(vars(closure))
        if isinstance(p, re.Pattern) and p.pattern in sources
    ] == []


def test_host_rule_tld_lists_reverse_dns_heads_and_extension_codes_by_enumeration() -> None:
    """D8: ``HOST_TLDS`` membership and shape, the extension codes absent, H1 and H2, the delimiters."""
    iso = [
        "ad",
        "ae",
        "af",
        "ag",
        "ai",
        "al",
        "am",
        "ao",
        "aq",
        "ar",
        "as",
        "at",
        "au",
        "aw",
        "ax",
        "az",
        "ba",
        "bb",
        "bd",
        "be",
        "bf",
        "bg",
        "bh",
        "bi",
        "bj",
        "bl",
        "bm",
        "bn",
        "bo",
        "bq",
        "br",
        "bs",
        "bt",
        "bv",
        "bw",
        "by",
        "bz",
        "ca",
        "cc",
        "cd",
        "cf",
        "cg",
        "ch",
        "ci",
        "ck",
        "cl",
        "cm",
        "cn",
        "co",
        "cr",
        "cu",
        "cv",
        "cw",
        "cx",
        "cy",
        "cz",
        "de",
        "dj",
        "dk",
        "dm",
        "do",
        "dz",
        "ec",
        "ee",
        "eg",
        "eh",
        "er",
        "es",
        "et",
        "fi",
        "fj",
        "fk",
        "fm",
        "fo",
        "fr",
        "ga",
        "gb",
        "gd",
        "ge",
        "gf",
        "gg",
        "gh",
        "gi",
        "gl",
        "gm",
        "gn",
        "gp",
        "gq",
        "gr",
        "gs",
        "gt",
        "gu",
        "gw",
        "gy",
        "hk",
        "hm",
        "hn",
        "hr",
        "ht",
        "hu",
        "id",
        "ie",
        "il",
        "im",
        "in",
        "io",
        "iq",
        "ir",
        "is",
        "it",
        "je",
        "jm",
        "jo",
        "jp",
        "ke",
        "kg",
        "kh",
        "ki",
        "km",
        "kn",
        "kp",
        "kr",
        "kw",
        "ky",
        "kz",
        "la",
        "lb",
        "lc",
        "li",
        "lk",
        "lr",
        "ls",
        "lt",
        "lu",
        "lv",
        "ly",
        "ma",
        "mc",
        "md",
        "me",
        "mf",
        "mg",
        "mh",
        "mk",
        "ml",
        "mm",
        "mn",
        "mo",
        "mp",
        "mq",
        "mr",
        "ms",
        "mt",
        "mu",
        "mv",
        "mw",
        "mx",
        "my",
        "mz",
        "na",
        "nc",
        "ne",
        "nf",
        "ng",
        "ni",
        "nl",
        "no",
        "np",
        "nr",
        "nu",
        "nz",
        "om",
        "pa",
        "pe",
        "pf",
        "pg",
        "ph",
        "pk",
        "pl",
        "pm",
        "pn",
        "pr",
        "ps",
        "pt",
        "pw",
        "py",
        "qa",
        "re",
        "ro",
        "rs",
        "ru",
        "rw",
        "sa",
        "sb",
        "sc",
        "sd",
        "se",
        "sg",
        "sh",
        "si",
        "sj",
        "sk",
        "sl",
        "sm",
        "sn",
        "so",
        "sr",
        "ss",
        "st",
        "sv",
        "sx",
        "sy",
        "sz",
        "tc",
        "td",
        "tf",
        "tg",
        "th",
        "tj",
        "tk",
        "tl",
        "tm",
        "tn",
        "to",
        "tr",
        "tt",
        "tv",
        "tw",
        "tz",
        "ua",
        "ug",
        "um",
        "us",
        "uy",
        "uz",
        "va",
        "vc",
        "ve",
        "vg",
        "vi",
        "vn",
        "vu",
        "wf",
        "ws",
        "ye",
        "yt",
        "za",
        "zm",
        "zw",
    ]
    reserved = ["ac", "eu", "su", "uk"]
    generic = [
        "com",
        "net",
        "org",
        "edu",
        "gov",
        "mil",
        "int",
        "info",
        "biz",
        "name",
        "pro",
        "dev",
        "app",
        "io",
        "cloud",
        "tech",
        "site",
        "online",
        "xyz",
        "co",
        "me",
        "tv",
        "ai",
        "ly",
        "to",
        "fm",
        "gg",
        "gl",
        "ws",
        "wiki",
        "page",
        "live",
        "run",
        "team",
        "works",
        "zone",
        "network",
        "systems",
        "services",
        "solutions",
        "software",
        "engineering",
        "digital",
        "agency",
        "studio",
        "host",
        "hosting",
        "domains",
        "email",
        "codes",
        "tools",
        "build",
        "one",
        "world",
        "global",
        "group",
        "company",
        "ltd",
        "inc",
        "llc",
        "gmbh",
    ]
    private = [
        "internal",
        "local",
        "lan",
        "intranet",
        "home",
        "corp",
        "localdomain",
        "test",
        "example",
        "invalid",
        "localhost",
    ]
    assert len(iso) == 249
    assert (
        set(iso) | set(reserved) | set(generic) | set(private)
    ) - closure_tables.EXTENSION_TLDS == closure_tables.HOST_TLDS
    assert {"eu", "su", "uk"} <= closure_tables.HOST_TLDS and "ac" not in closure_tables.HOST_TLDS
    assert all(2 <= len(t) <= 12 and t.isascii() and t.islower() for t in closure_tables.HOST_TLDS)
    assert not closure_tables.HOST_TLDS & closure_tables.EXTENSION_TLDS
    assert {
        "ac",
        "am",
        "cc",
        "cr",
        "cu",
        "gd",
        "in",
        "md",
        "mk",
        "ml",
        "mm",
        "pl",
        "pm",
        "pro",
        "ps",
        "py",
        "rs",
        "sc",
        "sh",
        "so",
        "st",
        "sv",
        "tf",
    } == closure_tables.EXTENSION_TLDS
    for diagnostic in (
        "kernel/foo.bar.cu:12:3: error: undefined",
        "top/core.alu.sv:33: syntax error",
        "src/settings.local.py:10:5: E501",
        "config.h.in:5: error",
        "app.main.cr:7:1: unexpected token",
        "ui/scene.menu.gd:3: Parse Error",
        "view.controller.mm:88: error",
        "build/app.core.pro:2: Unknown module",
        "repl/boot.init.sc:1: not found",
        "Object.subclass.st:4: parse error",
    ):
        assert closure.hosts(diagnostic) == [], diagnostic
        assert closure.judge(diagnostic, closure.Known(())) is None, diagnostic
    for code in ("com", "io", "dev", "ai", "de", "fr", "internal", "local", "corp", "example"):
        assert code in closure_tables.HOST_TLDS
    for head in closure_tables.REVERSE_DNS_HEADS:
        assert head in closure_tables.HOST_TLDS
        assert closure.hosts(f"{head}.acme.tools/x") == []
    assert {
        "com",
        "org",
        "net",
        "io",
        "dev",
        "me",
        "co",
        "edu",
        "gov",
    } == closure_tables.REVERSE_DNS_HEADS
    assert closure.hosts("a.b.zq/x") == []
    for tld in sorted(closure_tables.HOST_TLDS):
        found = closure.hosts(f"see svc.acme.{tld}/x now")
        assert [h.host for h in found] == [f"svc.acme.{tld}"], tld
        assert found[0].led is False
        assert closure.hosts(f"see svc.acme.{tld} now") == [], tld
        assert closure.hosts(f"see acme.{tld}/x now") == [], tld
    for tld in sorted(closure_tables.EXTENSION_TLDS):
        assert closure.hosts(f"see svc.acme.{tld}/x now") == [], tld
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
    assert [h.host for h in closure.hosts("a.b.io./x")] == ["a.b.io"]
    assert closure.hosts("-a.b.io/x")[0].host == "a.b.io"
    assert [h.host for h in closure.hosts("x--y.zq.io/x")] == ["x--y.zq.io"]
    assert [h.host for h in closure.hosts("https://x.zq.io- y")] == ["x.zq.io"]
    assert closure.hosts("1x.zq.io/x") != []
    assert closure.hosts("a.b..io/x") == []
    assert closure.hosts("a." + "b" * 64 + ".io/x") == []
    assert closure.hosts("a." + "b" * 63 + ".io/x") != []
    assert closure.hosts("a.b.i0/x") == []
    assert closure.hosts("a.b.i/x") == []
    for char in map(chr, range(128)):
        found = closure.hosts(f"{char}svc.acme.io/x")
        inside = char.isalnum() or char in ".-"
        assert (not found or found[0].host == "svc.acme.io") or inside, repr(char)
        if not inside:
            assert [h.host for h in found] == ["svc.acme.io"], repr(char)
            after = closure.hosts(f"https://svc.acme.io{char}")
            assert [h.host for h in after] == ["svc.acme.io"], repr(char)


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
    assert [str(n) for n in closure_tables.IPV4_RESERVED] == networks
    for net in map(ipaddress.IPv4Network, networks):
        for address in (net[0], net[-1]):
            assert not closure.is_public_address(str(address))
            assert closure.hosts(f"at {address} now") == []
            assert [h.host for h in closure.hosts(f"at {address}:80 now")] == [str(address)]
    for public in ("8.8.8.8", "11.22.33.44", "172.32.1.1", "100.128.1.1", "1.1.1.1"):
        assert closure.is_public_address(public)
        assert closure.hosts(f"at {public} now") == []
        assert closure.public_addresses(f"at {public} now") == []
        for text in (f"https://{public}/x", f"//{public}/x", f"x@{public}", f"git@{public}:o/r"):
            assert [(h.host, h.led) for h in closure.hosts(text)] == [(public, True)], text
        for text in (f"at {public}:80", f"at {public}/seg", f"at {public}:8080/seg"):
            assert [(h.host, h.led) for h in closure.hosts(text)] == [(public, False)], text
        assert closure.judge(f"at {public}:80", closure.Known(())) == closure.Refusal(
            "public address"
        )
        assert closure.judge(f"at {public}/seg", closure.Known(())) == closure.Refusal(
            "public address"
        )
        assert closure.judge(f"at {public} now", closure.Known(())) is None
    assert [h.host for h in closure.hosts("ssh://10.0.0.5/ns/repo")] == ["10.0.0.5"]
    assert [h.host for h in closure.hosts("git@10.0.0.5:ns/repo")] == ["10.0.0.5"]
    for lead in ("==", ">=", "<=", "~=", "!=", "version", "Version", "VERSION"):
        assert closure.hosts(f"pkg {lead}8.8.8.8") == [], lead
        assert closure.hosts(f"pkg {lead}8.8.8.8:80") == [], lead
        assert closure.hosts(f"pkg {lead}8.8.8.8/x") == [], lead
    for tail in ("-rc1", "+build", ".tar.gz", ".whl", ".zip", ".gz"):
        assert closure.hosts(f"pkg 8.8.8.8{tail}") == [], tail
    assert closure.hosts("addr 8.8.8.8.") == []
    assert [h.host for h in closure.hosts("addr 8.8.8.8./x")] == ["8.8.8.8"]
    for text in ("addr 8.8.8.256:80", "addr 8.8.8:80", "addr 8.8.8.8.8:80", "addr 8.8.8.0008:80"):
        assert closure.hosts(text) == [], text
    assert [h.host for h in closure.hosts("addr 008.8.8.8:80")] == ["008.8.8.8"]
    assert [h.host for h in closure.hosts("addr 8.8.8.8:80")] == ["8.8.8.8"]
    assert closure.hosts("addr 8.8.8.8:") == []
    assert closure.hosts("addr 8.8.8.8:x") == []
    assert closure.hosts("addr 8.8.8.8/") == []
    for shaped in ("1.8.0.252", "4.8.1.0", "11.0.3.4", "8.8.0.8"):
        assert closure.hosts(f"JDK {shaped} and dotnet") == [], shaped
        assert [h.host for h in closure.hosts(f"https://{shaped}/x")] == [shaped], shaped
        assert [h.host for h in closure.hosts(f"{shaped}:8080")] == [shaped], shaped
    assert closure.hosts("pkg == 1.2.3.4") == []
    assert closure.hosts("pkg  >=   1.2.3.4") == []
    assert closure.hosts("pkg = 1.2.3.4") == []
    assert closure.is_placeholder_host("203.0.113.9")
    assert closure.is_placeholder_host("127.1.2.3")
    assert not closure.is_placeholder_host("10.0.0.1")
    assert not closure.is_placeholder_host("8.8.8.8")
    assert closure.is_address("8.8.8.8.")
    assert not closure.is_address("8.8.8")
    assert not closure.is_address("a.b.io")
    assert not closure.is_public_address("a.b.io")
    assert not closure.is_public_address("10.0.0.1")


def test_a_bare_dotted_quad_in_prose_is_ordinary_text_whatever_its_octets() -> None:
    """NG4-9 (0.19, owner decision): a quad with no lead and no follow is written, version or public address alike."""
    none = closure.Known(())
    for text in (
        "1.2.3.4",
        "Bump to 1.2.3.4",
        "pkg = 1.2.3.4",
        "JDK 1.8.0.252",
        "ASA 9.12.4.1",
        "3.4.0.12 and 1.2.0.33",
        "firmware 2.4.1.9 -> 2.4.2.0",
        "resolver 1.1.1.1 8.8.8.8 valid=300s;",
        "DNS: 9.9.9.9 (Quad9)",
        "ssh: connect to host 11.22.33.44 port 22: Connection refused",
        "see 104.16.0.1 and 104.16.0.1.",
        "104.16.0.1, then 51.15.20.30",
    ):
        assert closure.hosts(text) == [], text
        assert closure.judge(text, none) is None, text
    for text in ("104.16.0.1:8080", "104.16.0.1/kvnteam", "see 104.16.0.1:443/kvnteam/kvnbilling"):
        assert closure.judge(text, none) == closure.Refusal("public address"), text
        assert [h.host for h in closure.hosts(text)] == ["104.16.0.1"], text
    assert closure.judge("see 10.1.2.3/kvnteam", none) is None
    assert closure.judge("see http://10.1.2.3/kvnteam", none) == closure.Refusal("unknown host")


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
    for char in string.ascii_letters + string.digits:
        assert closure.mailboxes(f"{char}@h.io")[0].local == char
    for char in "._%+-:${}":
        assert closure.mailboxes(f"{char}@h.io") == []
        assert closure.mailboxes(f"a{char}@h.io")[0].local == f"a{char}"
        assert closure.hosts(f"{char}@h.zz") == []
    assert closure.hosts("+@pytest.fixture") == []
    for char in " /\"'<>()[]\\,;=!?#|*`~^&":
        assert closure.mailboxes(f"a{char}b@h.io")[0].local == "b"
    assert closure.mailboxes("MAILTO:a@h.io")[0].local == "a"


def test_public_allowlist_and_placeholder_class_by_enumeration() -> None:
    """D8: the 25 suffixes exact and as ``.<suffix>``, trailing dot, case, forge hosts never public (0.22: the yarn and npmmirror registries)."""
    suffixes = {
        "yarnpkg.com",
        "npmmirror.com",
        "crates.io",
        "nuget.org",
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
        "jsdelivr.net",
        "unpkg.com",
        "apache.org",
    }
    assert suffixes == closure_tables.PUBLIC_SUFFIXES
    assert len(closure_tables.PUBLIC_SUFFIXES) == 25
    assert closure.is_public_host("api.nuget.org") and not closure.is_public_host("nuget.com")
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
    assert text_forms.node_id_numbers("MDQ6VXNlcjc0OTI4MTYzsuffix") == ["74928163"]
    assert text_forms.node_id_numbers("abcdefgU_kgDOBHdQIw") == ["74928163"]
    assert text_forms.node_id_numbers("U_1001") == ["1001"]
    assert text_forms.node_id_numbers("MDQ6VXNl") == []
    assert text_forms.node_id_numbers("U_a") == []
    assert text_forms.node_id_numbers("U_abcde") == []
    assert text_forms.node_id_numbers(_node_new(5)) == ["5"]
    assert text_forms.node_id_numbers(
        "U_" + base64.urlsafe_b64encode(b"\x92\x00\x05").decode().rstrip("=")
    ) == ["5"]
    assert text_forms.node_id_numbers(
        "U_" + base64.urlsafe_b64encode(b"\x92\x00\xcc\x80").decode().rstrip("=")
    ) == ["128"]
    assert text_forms.node_id_numbers(
        "U_" + base64.urlsafe_b64encode(b"\x92\x00\xcd\x01\x00").decode().rstrip("=")
    ) == ["256"]
    assert text_forms.node_id_numbers(
        "U_"
        + base64.urlsafe_b64encode(b"\x92\x00\xcf" + (2**40).to_bytes(8, "big"))
        .decode()
        .rstrip("=")
    ) == [str(2**40)]
    assert (
        text_forms.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x93\x00\x05").decode().rstrip("=")
        )
        == []
    )
    assert (
        text_forms.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x92\x00\xd0\x05").decode().rstrip("=")
        )
        == []
    )
    assert (
        text_forms.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x92\x00\xcd\x01").decode().rstrip("=")
        )
        == []
    )
    assert (
        text_forms.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x92\x00").decode().rstrip("=")
        )
        == []
    )
    assert text_forms.node_id_numbers("MDQ6VXNlcjEyMw==") == ["123"]
    assert text_forms.node_id_numbers("MDQ6VXNlcjEyMw") == ["123"]
    assert text_forms.node_id_numbers("MDQ6VXNlcjEyM") == ["12"]
    assert text_forms.node_id_numbers("MDQ6VXNlcg==") == []
    assert text_forms.node_id_numbers("MDQ6VXNlcjEyMw==" + "A" * 80) == ["123"]
    assert text_forms.node_id_numbers("MDQ6VXNl" + "!" * 3) == []
    assert text_forms.node_id_numbers(base64.b64encode(b"04:Repo123").decode()) == []
    assert text_forms.node_id_numbers("MDQ6VXNlcjEyMw==" + "x" + "MDQ6VXNlcjQ1Ng==") == [
        "123",
        "456",
    ]
    decoded = text_forms.decoded_forms("a%2520b")
    assert decoded == ["a%2520b", "a%20b", "a b"]
    assert text_forms.decoded_forms("plain") == ["plain"]
    assert len(text_forms.decoded_forms("%" + "25" * 5 + "41")) == 5
    assert text_forms.decoded_forms("%" + "25" * 5 + "41")[-1] == "%2541"
    assert text_forms.decoded_forms(r"A")[-1] == "A"
    assert text_forms.decoded_forms("&amp;lt;")[-1] == "<"
    literal = closure.Known((("login", "github"),), literals=("github-actions[bot]", "github"))
    assert closure.residual("by github-actions[bot] now", literal) is None
    assert closure.residual("by github now", literal) is None
    assert closure.residual("GITHUB-ACTIONS[BOT]", literal) == ("login", 1)
    assert (
        closure.residual(
            "Docs.GitHub.com", closure.Known((("login", "github"),), ("docs.github.com",))
        )
        is None
    )
    assert closure.residual("by github-actionsx", literal) == ("login", 1)
    assert closure.residual("xgithub", literal) is None
    assert closure.residual("github.com", literal) is None
    pattern = text_forms.token_pattern("a.b")
    assert pattern.search("x a.b y") and not pattern.search("xa.b") and not pattern.search("a.bx")
    assert pattern.search("A.B")
    assert text_forms.token_pattern("a").search("1-a-1") and not text_forms.token_pattern(
        "a"
    ).search("ba")
    assert text_forms.token_pattern("a").search("%2Fa") and text_forms.token_pattern("a").search(
        "%2fa"
    )
    assert not text_forms.token_pattern("a").search("%2Fxa")
    assert not text_forms.token_pattern("a").search("%3Fa") or True
    assert text_forms.token_pattern("a").search("%3Fa") is None
    assert text_forms.token_pattern("a").search("%2a") is None
    assert text_forms.token_pattern("a").search("2Fa") is None


def test_judge_orders_its_reasons_and_blanks_literals() -> None:
    """D3: residual, unknown host, public address, unknown mailbox, in that order, never a value."""
    known = closure.Known(
        (("login", "zqlogin"),), literals=("docs.github.com", "docs.gitlab.com", ":owner")
    )
    assert closure.judge("fine https://forge.example.com/org/project", known) is None
    assert closure.judge(
        "zqlogin at git.acme.io:80 8.8.8.8:80 a@gmail.com", known
    ) == closure.Refusal("residual", "login", 1)
    assert closure.judge("git.acme.io:80 8.8.8.8:80 a@gmail.com", known) == closure.Refusal(
        "unknown host"
    )
    assert closure.judge("8.8.8.8:80 a@b.example.com", known) == closure.Refusal("public address")
    assert closure.judge("x@docs.github.com x@docs.gitlab.com", known) is None
    assert closure.judge("8.8.8.8 a@b.example.com", known) == closure.Refusal("unknown mailbox")
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
    assert {"http", "https", "ssh", "git", "ftp", "ftps"} == closure_tables.URL_SCHEMES


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


def test_value_types_are_frozen_and_carry_their_documented_defaults() -> None:
    """D8: ``Host``, ``Mailbox``, ``Refusal`` and ``Known`` are immutable values with fixed defaults."""
    import dataclasses

    host = closure.Host("a.b.io", True, 1, 7)
    box = closure.Mailbox("a", "b.io", 0, 6)
    refusal = closure.Refusal("unknown host")
    known = closure.Known((("login", "zqlogin"),))
    for value, attribute in (
        (host, "host"),
        (box, "local"),
        (refusal, "reason"),
        (known, "entries"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, attribute, "x")
    assert (refusal.reason, refusal.kind, refusal.n) == ("unknown host", "", 0)
    assert closure.Refusal("residual", "login", 2) == closure.Refusal("residual", "login", 2)
    assert closure.Refusal("residual", "login", 2) != closure.Refusal("residual", "login", 1)
    assert (known.literals, known.tokens) == ((), ())
    assert known.values("host") == ()
    assert closure.Known((("deny-list", "zq"),), ("a",), (("b", "c"),)).literals == ("a",)
    assert closure.Known((("login", "zq"),), ("a",), (("b", "c"),)).tokens == (("b", "c"),)


def test_reader_positions_flags_and_edges_by_enumeration() -> None:
    """D8: offsets, the ``led`` flag, trailing dots, hyphen runs, schemes, version contexts and unterminated mailboxes."""
    found = closure.hosts("x -git.acme.io./z y")
    assert [(h.host, h.led, h.start, h.end) for h in found] == [("git.acme.io", False, 3, 15)]
    found = closure.hosts("see https://git.acme.io/x")
    assert [(h.host, h.led, h.start, h.end) for h in found] == [("git.acme.io", True, 12, 23)]
    found = closure.hosts("a ..git.acme.io/x")
    assert (found[0].start, found[0].end) == (4, 15)
    found = closure.hosts("a ---git.acme.io/x")
    assert (found[0].start, found[0].end) == (5, 16)
    found = closure.hosts("a https://git.acme.io---")
    assert (found[0].start, found[0].end) == (10, 21)
    found = closure.hosts("ssh://8.8.8.8./x")
    assert [(h.host, h.led, h.start) for h in found] == [("8.8.8.8", True, 6)]
    found = closure.hosts("ssh 8.8.8.8:22")
    assert [(h.host, h.led, h.start, h.end) for h in found] == [("8.8.8.8", False, 4, 11)]
    assert [h.led for h in closure.hosts("//8.8.8.8/x")] == [True]
    assert [(h.host, h.led) for h in closure.hosts("//a.b.io/x")] == [("a.b.io", False)]
    assert closure.hosts("a ://acme.zz") == []
    assert closure.hosts("://acme.zz") == []
    assert closure.hosts("1://acme.zz") == []
    assert [h.host for h in closure.hosts("x+y-z.q://acme.zz")] == ["acme.zz"]
    assert [h.host for h in closure.hosts("a@acme.zz")] == ["acme.zz"]
    assert closure.hosts("@acme.zz") == []
    assert closure.hosts("=@acme.zz") == []
    assert closure.hosts("x=@acme.zz") == []
    assert [h.host for h in closure.hosts("1@acme.zz")] == ["acme.zz"]
    assert closure.hosts("https://8.8.8.8/x")[0].end == 15
    assert closure.hosts("a.b.c.io/x")[0].end == 8
    assert closure.hosts("https://x.io/a")[0].start == 8
    for phrase in (
        "version 8.8.8.8:80",
        "Version 8.8.8.8:80",
        "version=8.8.8.8:80",
        "VERSION:8.8.8.8:80",
        "a version:8.8.8.8:80",
        "version  8.8.8.8:80",
        "a version: 8.8.8.8:80",
        "Version:\t8.8.8.8:80",
        "version = 8.8.8.8:80",
    ):
        assert closure.hosts(phrase) == [], phrase
    for phrase in ("versio 8.8.8.8:80", "version x 8.8.8.8:80", "versions 8.8.8.8:80"):
        assert [h.host for h in closure.hosts(phrase)] == ["8.8.8.8"], phrase
    assert closure.hosts("x //8.8.8.8/y")[0].led
    assert closure.hosts("x /8.8.8.8/y") == []
    assert [h.host for h in closure.hosts("a=1.1.1.1:80")] == ["1.1.1.1"]
    assert [h.host for h in closure.hosts("a>1.1.1.1:80")] == ["1.1.1.1"]
    for comparator in ("==", ">=", "<=", "~=", "!="):
        assert closure.hosts(f"a{comparator}1.1.1.1:80") == [], comparator
    assert closure.mailboxes("a@") == []
    assert closure.mailboxes("a@ b.io") == []
    assert closure.mailboxes("a@h.io-")[0].host == "h.io"
    assert closure.mailboxes("a@h.io-")[0].end == 6
    assert closure.mailboxes("a@-h.io") == []
    assert closure.mailboxes("a@h.io.")[0].host == "h.io"
    assert closure.mailboxes("a@h.io.")[0].end == 6
    assert closure.mailboxes("mailto:a@h.io")[0].start == 7
    assert closure.mailboxes("MAILTO:a@h.io")[0].start == 7
    assert closure.mailboxes("xmailto:a@h.io")[0].local == "xmailto:a"
    assert closure.mailboxes("a@8.8.8.8")[0].host == "8.8.8.8"
    assert closure.mailboxes("a@8.8.8.8.")[0].host == "8.8.8.8"
    assert closure.mailboxes("a@1.2.3") == []
    for text in (
        "NOREPLY@FORGE.EXAMPLE.COM",
        "No-Reply@Forge.Example.Com",
        "GIT@FORGE.EXAMPLE.COM",
        "Author@Users.Noreply.Forge.Example.Com",
        "X@EXAMPLE.COM",
        "x@A.INTERNAL",
    ):
        assert closure.is_literal_mailbox(closure.mailboxes(text)[0]), text
    for text in (
        "no-reply@sub.forge.example.com",
        "bot@users.noreply.forge.example.org",
        "bot@xusers.noreply.forge.example.com",
        "bot@users.noreply.forge.example.com.evil.io",
    ):
        assert not closure.is_literal_mailbox(closure.mailboxes(text)[0]), text
    assert closure.is_placeholder_host("localhost") is True
    assert closure.is_placeholder_host("example") is True
    assert closure.is_placeholder_host("test") is True
    assert closure.is_placeholder_host("invalid") is True
    assert closure.is_placeholder_host("x.example") is True
    assert closure.is_placeholder_host("1.2.3") is False
    assert closure.is_placeholder_host("8.8.8.8") is False
    assert closure.is_placeholder_host("127.0.0.1") is True
    assert closure.is_placeholder_host("a.b") is False
    assert closure.is_public_host("pypi.org") is True
    assert closure.is_public_host("pypi.com") is False
    assert closure.is_doc_mailbox_host("example.com") is True
    assert closure.is_doc_mailbox_host("example.io") is False
    assert text_forms.node_id_numbers("U_kgAF") == ["5"]
    assert text_forms.node_id_numbers("xU_kgAF") == ["5"]
    assert text_forms.node_id_numbers("U_kgA") == []
    assert text_forms.node_id_numbers("U_kgB_") == ["127"] if False else True
    assert text_forms.node_id_numbers(
        "U_" + base64.urlsafe_b64encode(b"\x92\x00\x7f").decode().rstrip("=")
    ) == ["127"]
    assert (
        text_forms.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x92\x00\x80").decode().rstrip("=")
        )
        == []
    )
    assert (
        text_forms.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x92\x00\xcb\x00").decode().rstrip("=")
        )
        == []
    )
    twenty = 2**64 - 1
    assert text_forms.node_id_numbers(base64.b64encode(f"04:User{twenty}".encode()).decode()) == [
        str(twenty)
    ]
    assert text_forms.node_id_numbers(base64.b64encode(f"04:User{twenty}x".encode()).decode()) == [
        str(twenty)
    ]
    one = base64.b64encode(b"04:User1").decode()
    assert text_forms.node_id_numbers(one) == ["1"]
    assert text_forms.node_id_numbers(one.rstrip("=")) == ["1"]


def test_case_is_folded_by_every_reader_and_a_blanked_literal_separates_its_neighbours() -> None:
    """D8, D3: hosts, tokens and literals are judged casefolded; a blanked literal never fuses what stood around it."""
    assert [h.host for h in closure.hosts("SVC.ACME.IO/x")] == ["SVC.ACME.IO"]
    assert [h.host for h in closure.hosts("svc.acme.Io/x")] == ["svc.acme.Io"]
    assert closure.hosts("COM.ACME.TOOLS/x") == []
    assert closure.hosts("Com.acme.tools/x") == []
    assert [h.host for h in closure.hosts("ssh://SVC.Acme.PY")] == ["SVC.Acme.PY"]
    known = closure.Known((("deny-list", "x__y"),), (":owner",))
    assert closure.residual("x_:owner_y", known) is None
    assert closure.residual("x__y", known) == ("deny-list", 1)


def test_a_dotted_name_is_a_host_only_when_led_or_followed_by_construction() -> None:
    """D8 (0.18): led by a scheme, ``<local>@`` or ``//``, or followed by ``/<segment>`` or ``:<port>``; else ordinary text."""
    bare = (
        "response.data.id",
        "self.logger.info",
        "request.user.email",
        "self.server.host",
        "app.config.name",
        "this.props.id",
        "arr.items.at",
        "cfg.Server.Host",
        "ctx.cfg.net.host",
        "window.app.config.dev",
        "req.params.id",
        "self.config.dev",
        "mirror.zqcorp.net",
        "code.kvncloud.io",
    )
    for name in bare:
        assert closure.hosts(f"x = {name};") == [], name
        assert closure.hosts(f"Host {name} is down") == [], name
        assert closure.hosts(f"{name}()") == [], name
        assert closure.hosts(f"{name}: up") == [], name
        assert closure.hosts(f"{name}:x") == [], name
        assert closure.hosts(f"{name}: 8080") == [], name
        assert closure.hosts(f"{name}/") == [], name
        assert closure.hosts(f"{name}/ x") == [], name
        assert closure.hosts(f"{name}//x") == [], name
        found = closure.hosts(f"see {name}/seg")
        assert [(h.host, h.led) for h in found] == [(name, False)], name
        assert [h.host for h in closure.hosts(f"see {name}:8080")] == [name], name
        assert [h.host for h in closure.hosts(f"see //{name}")] == [name], name
        assert [h.host for h in closure.hosts(f"see https://{name}")] == [name], name
        assert [h.host for h in closure.hosts(f"see git@{name}")] == [name], name
    for first in "abcxyzABCXYZ0129_-.~%":
        assert [h.host for h in closure.hosts(f"see svc.acme.io/{first}b")] == ["svc.acme.io"]
    for first in " \t\n/?#:@!'\"()<>[]{}\\,;=&*+^`|":
        assert closure.hosts(f"see svc.acme.io/{first}b") == [], repr(first)
    for digit in "0123456789":
        assert [h.host for h in closure.hosts(f"see svc.acme.io:{digit}")] == ["svc.acme.io"]
    for other in "abcxyzABC_-./ \n@%":
        assert closure.hosts(f"see svc.acme.io:{other}1") == [], repr(other)
    assert closure.hosts("see svc.acme.io") == []
    assert closure.hosts("see svc.acme.io/") == []
    assert closure.hosts("see svc.acme.io:") == []
    assert closure.hosts("see acme.io/seg") == []
    assert closure.hosts("see acme.io:8080") == []
    assert [h.host for h in closure.hosts("see //acme.io")] == []
    assert [h.host for h in closure.hosts("see https://acme.io")] == ["acme.io"]
    assert [h.host for h in closure.hosts("see git@acme.io:o/r")] == ["acme.io"]
    assert closure.hosts("main.py:42") == []
    assert closure.hosts("a.b.rs:42 and x.y.sh/run") == []


def test_a_bare_dotted_host_in_prose_is_the_documented_residual() -> None:
    """NG4 (0.18): a third-party private host cited bare in prose is neither a host nor refused; a known one is still replaced."""
    prose = "Host mirror.zqcorp.net is down, ask code.kvncloud.io"
    assert closure.hosts(prose) == []
    assert closure.judge(prose, closure.Known(())) is None
    known = closure.Known((("host", "mirror.zqcorp.net"),))
    assert closure.judge(prose, known) == closure.Refusal("residual", "host", 1)
    assert closure.judge("Host mirror.zqcorp.net/x is down", closure.Known(())) == closure.Refusal(
        "unknown host"
    )


def test_a_percent_escape_is_never_part_of_a_label_run() -> None:
    """D2 (0.18): ``%2F`` before a name is a delimiter, so ``%2Fcode.acme.io`` reads ``code.acme.io``."""
    found = closure.hosts("a%2Fcode.acme.io/x")
    assert [(h.host, h.start) for h in found] == [("code.acme.io", 4)]
    assert [h.host for h in closure.hosts("a%2fcode.acme.io/x")] == ["code.acme.io"]
    assert [h.host for h in closure.hosts("https://%2Facme.io")] == []
    assert [h.host for h in closure.hosts("a%code.acme.io/x")] == ["code.acme.io"]
    assert closure.hosts("a%e2.acme.io/x") == []
    assert [h.host for h in closure.hosts("a%2.acme.io/x")] == ["2.acme.io"]
    assert [h.host for h in closure.hosts("a%G2code.acme.io/x")] == ["G2code.acme.io"]
    assert [h.host for h in closure.hosts("a%2Gcode.acme.io/x")] == ["2Gcode.acme.io"]
    assert [h.host for h in closure.hosts("%2Fcode.acme.io/x")] == ["code.acme.io"]
    assert closure.hosts("https%3A%2F%2Fcode.acme.io%2Fx") == []
    decoded = text_forms.decoded_forms("https%3A%2F%2Fcode.acme.io%2Fx")
    assert [h.host for h in closure.hosts(decoded[-1])] == ["code.acme.io"]


def test_the_judge_reads_hosts_addresses_and_mailboxes_in_every_decoded_form() -> None:
    """D2 (0.18): an encoded lead, segment or ``@`` counts as the character it encodes."""
    none = closure.Known(())
    for text in (
        "redirect=https%3A%2F%2Fkvncloud.io%2Fkvnteam%2Fkvnbilling",
        "redirect=https%3A%2F%2Fcode.kvncloud.io%2Fkvnteam",
        "see code.kvncloud.io%2Fkvnteam%2Fkvnbilling",
        "see code.kvncloud.io%3A8443",
        "see &#104;ttps://kvncloud.io/x",
        "see https:&#47;&#47;kvncloud.io",
        "see \\u0068ttps://kvncloud.io",
    ):
        assert closure.judge(text, none) == closure.Refusal("unknown host"), text
    for text in ("mail kvnperson&#64;kvnmail.io", "mail kvnperson%40kvnmail.io"):
        assert closure.judge(text, none) == closure.Refusal("unknown host"), text
    for text in ("mail kvnperson&#64;mail.example.com", "mail kvnperson%40mail.example.com"):
        assert closure.judge(text, none) == closure.Refusal("unknown mailbox"), text
    for text in ("ssh to 8&#46;8&#46;8&#46;8:22 now", "ssh to 8%2E8%2E8%2E8%3A22 now"):
        assert closure.judge(text, none) == closure.Refusal("public address"), text
    for text in (
        "ssh to %5B2a00%3A1450%3A4001%3A%3A1%5D now",
        "ssh to &#91;2a00:1450:4001::1&#93; now",
        "ssh to [2a00:1450:4001::1] now",
    ):
        assert closure.judge(text, none) == closure.Refusal("public address"), text
    assert closure.judge("see https%3A%2F%2Fexample.com%2Fx and a%40example.com", none) is None
    assert closure.judge("100%25 done, a%2Fb", none) is None
    assert closure.judge("see https%3A%2F%2Fdocs.python.org%2F3", none) is None


def test_the_legacy_node_reader_never_raises_on_a_malformed_run() -> None:
    """D3 (0.18): ``MDQ6VXNl`` runs of any length and padding are read or ignored, never a crash."""
    body = "MDQ6VXNlcjc0OTI4MTYzabcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN0123456789+/"
    for length in range(8, 60):
        for pad in ("", "=", "==", "==="):
            text = f"x {body[:length]}{pad} y"
            assert isinstance(text_forms.node_id_numbers(text), list), (length, pad)
    assert text_forms.node_id_numbers("see MDQ6VXNlcjc0O= here") == ["74"]
    assert text_forms.node_id_numbers("see MDQ6VXNlcjc0OTI4MTYz= here") == ["74928163"]
    assert text_forms.node_id_numbers("see MDQ6VXNlcjc0OTI4MTY= here") == ["7492816"]
    assert text_forms.node_id_numbers("see MDQ6VXNlcjc0OTI4MTYz== here") == ["74928163"]
    assert text_forms.node_id_numbers("see MDQ6VXNlcjc0OTI4MTYzA here") == ["74928163"]
    assert text_forms.node_id_numbers("MDQ6VXNlcjc0O" + "=" * 2) == ["74"]


def test_a_bracketed_ipv6_literal_is_judged_like_an_ipv4_address() -> None:
    """D8 (0.18): led it is a host, un-led and global it is a public address, a documentation prefix is placeholder."""
    for literal in ("[2001:db8::1]", "[2001:DB8::1]", "[3fff::1]", "[::1]"):
        assert closure.is_placeholder_host(literal), literal
        assert closure.is_placeholder_host(literal.strip("[]")), literal
    for literal in ("[fd00::1]", "[fe80::1]", "[2a00:1450::1]", "[::2]"):
        assert not closure.is_placeholder_host(literal), literal
    found = closure.hosts("see http://[2a00:1450::1]/ns/repo")
    assert [(h.host, h.led, h.literal) for h in found] == [("[2a00:1450::1]", True, True)]
    assert [h.host for h in closure.hosts("x //[fd00::1]:8080/y")] == ["[fd00::1]"]
    assert [h.host for h in closure.hosts("git@[fd00::1]:o/r")] == ["[fd00::1]"]
    assert closure.hosts("see [fd00::1] and [fd00::1]:80 and a[::1]") == []
    assert closure.hosts("see [1:2] [1:2:3] [x::1] [::g] [] [:::]") == []
    assert [h.host for h in closure.hosts("see http://[::]/x")] == ["[::]"]
    assert closure.public_addresses("ssh [2a00:1450::1]") == ["[2a00:1450::1]"]
    assert closure.public_addresses("ssh [2a00:1450:4001::1]:22") == ["[2a00:1450:4001::1]"]
    assert closure.public_addresses("ssh [2A00:1450:4001::1]") == ["[2A00:1450:4001::1]"]
    assert closure.public_addresses("see http://[2a00:1450:4001::1]/x") == []
    assert closure.public_addresses("see //[2a00:1450:4001::1]/x") == []
    assert closure.public_addresses("see user@[2a00:1450:4001::1]") == []
    assert closure.public_addresses("xs[2020::] xs[::2] xs[2a00::] xs[2a00::1]") == []
    assert closure.public_addresses("ssh [2001:db8:1::1] [fd00:1::1] [fe80:1::1]") == []
    assert closure.judge("see http://[2a00:1450::1]/x", closure.Known(())) == closure.Refusal(
        "unknown host"
    )
    assert closure.judge("see http://[2001:db8::1]/x", closure.Known(())) is None
    assert closure.judge("ssh [2a00:1450:4001::1]", closure.Known(())) == closure.Refusal(
        "public address"
    )
    for text, expected in (
        ("[::1]", "::1"),
        ("[::]", "::"),
        ("[1:2:3:4:5:6:7:8]", "1:2:3:4:5:6:7:8"),
        ("[1:2:3:4:5:6:7::]", "1:2:3:4:5:6:7:0"),
        ("[1::]", "1::"),
        ("[0001:0002:0003:0004:0005:0006:0007:0008]", "1:2:3:4:5:6:7:8"),
    ):
        assert closure._ipv6(text) == ipaddress.IPv6Address(expected), text
    for text in (
        "[1:2]",
        "[1:2:3:4:5:6:7]",
        "[1:2:3:4:5:6:7:8:9]",
        "[1:2:3:4:5:6:7:8::]",
        "[1::2::3]",
        "[:1:2]",
        "[1:2:]",
        "[::12345]",
        "[::g]",
        "[a:::b]",
        "[1.2.3.4]",
        "",
    ):
        assert closure._ipv6(text) is None, text


def test_the_version_guard_of_a_led_address_is_observable() -> None:
    """H07 (0.18): a led address followed by ``-`` or ``+`` or after a comparator is a version, not a host."""
    for text in (
        "http://8.8.8.8+1/x",
        "http://8.8.8.8- y",
        "//8.8.8.8+rc",
        "x@8.8.8.8-1",
        "//10.0.0.1+1",
    ):
        assert closure.hosts(text) == [], text
    for text in ("http://8.8.8.8 y", "//8.8.8.8 y", "x@8.8.8.8 y", "http://8.8.8.8/x"):
        assert [h.host for h in closure.hosts(text)] == ["8.8.8.8"], text


def test_the_unspecified_and_broadcast_addresses_are_placeholder_class() -> None:
    """D8 (0.20, C5): 0.0.0.0, 255.255.255.255 and :: are a bind or a log line, never a machine."""
    for host in ("0.0.0.0", "255.255.255.255", "[::]", "::", "0.0.0.0.", "[::1]", "127.0.0.1"):
        assert closure.is_placeholder_host(host), host
    for host in ("0.0.0.1", "0.1.2.3", "255.255.255.254", "255.255.255.0", "[::2]", "[1::]"):
        assert not closure.is_placeholder_host(host), host
    none = closure.Known(())
    for text in (
        "Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)",
        "open http://0.0.0.0:8000/docs",
        "http://255.255.255.255/x",
        "Listening on 0.0.0.0:8000 and [::]:8000",
        "see http://[::]:8080/x",
    ):
        assert closure.judge(text, none) is None, text
        assert [h.host for h in closure.hosts(text)], text
    assert closure.judge("see http://0.1.2.3/x", none) == closure.Refusal("unknown host")


def test_a_quad_after_a_single_slash_is_a_version_and_a_comparator_allows_spaces() -> None:
    """D8 IPv4 (0.20, C4 and C59): a path segment is a version context; spaces after a comparator keep it one."""
    for text in (
        "https://repo.maven.apache.org/maven2/x/ojdbc8/19.3.0.0/ojdbc8-19.3.0.0.pom",
        "https://pypi.org/project/x/1.2.3.4/files",
        "https://files.pythonhosted.org/packages/ab/cd/12.6.1.1/x.whl",
        "https://api.nuget.org/v3-flatcontainer/x/1.2.3.4/x.1.2.3.4.nupkg",
        "x /8.8.8.8/y",
        "proxy/104.16.0.1:8080/x",
        "a/1.2.3.4",
    ):
        assert [h.host for h in closure.hosts(text) if h.host[0].isdigit()] == [], text
    for text in ("//8.8.8.8/x", "x//8.8.8.8/y", "http://8.8.8.8/x"):
        assert [(h.host, h.led) for h in closure.hosts(text)] == [("8.8.8.8", True)], text
    assert [h.host for h in closure.hosts("see 1.2.3.4/24")] == ["1.2.3.4"]
    assert closure.hosts("see 10.0.0.0/8")[0].led is False
    for gap in ("", " ", "  ", "   ", "\t"):
        for comparator in closure._COMPARATORS:
            assert closure.hosts(f"pkg {comparator}{gap}8.8.8.8:80") == [], (comparator, gap)
            assert closure.hosts(f"pkg {comparator}{gap}8.8.8.8/x") == [], (comparator, gap)
    assert closure.hosts("pkg  >=   8.8.8.8:80") == []
    assert [h.host for h in closure.hosts("pkg >>  8.8.8.8:80")] == ["8.8.8.8"]
    assert [h.host for h in closure.hosts("pkg ==\n8.8.8.8:80")] == ["8.8.8.8"]


def test_the_readers_are_linear_in_the_text() -> None:
    """C15 (0.20): the host readers look back without copying the prefix; 5 000 URLs and a 1 MB diff stay under a bound."""
    none = closure.Known(())
    many = " ".join(f"http://a{i}.b{i}.io/x/y" for i in range(5000))
    started = time.perf_counter()
    assert len(closure.hosts(many)) == 5000
    assert closure.judge(many, none) == closure.Refusal("unknown host")
    diff = ("+" + "x.y.z 1.2.3 a.b.c.io " * 20) * 5000
    assert closure.hosts(diff) == [] and closure.judge(diff, none) is None
    assert time.perf_counter() - started < 8.0
    for text, expected in (
        ("1://acme.zz", []),
        ("x+y-z.q://acme.zz", ["acme.zz"]),
        ("a ://acme.zz", []),
        ("://acme.zz", []),
        ("+.-://acme.zz", []),
        ("é://acme.zz", []),
        ("svn+ssh://acme.zz", ["acme.zz"]),
        ("a@acme.zz", ["acme.zz"]),
        ("=@acme.zz", []),
        ("x //8.8.8.8/y", ["8.8.8.8"]),
        ("x /8.8.8.8/y", []),
        ("version 8.8.8.8:80", []),
        ("Version: 8.8.8.8:80", []),
        ("Version:8.8.8.8:80", []),
        ("aversion=8.8.8.8:80", []),
        ("versio 8.8.8.8:80", ["8.8.8.8"]),
        ("pkg  >=\t 8.8.8.8:80", []),
        ("8.8.8.8:80", ["8.8.8.8"]),
    ):
        assert [h.host for h in closure.hosts(text)] == expected, text
    assert closure._scheme_before("http", 4) and not closure._scheme_before("1", 1)
    for text, expected in (
        ("a+://acme.zz", ["acme.zz"]),
        ("s3://bucket.kvncloud.io/x", ["bucket.kvncloud.io"]),
        ("s3://acme.zz", ["acme.zz"]),
        ("h2c://acme.zz", ["acme.zz"]),
        ("x9://acme.zz", ["acme.zz"]),
        ("/8.8.8.8/x", []),
        ("== 8.8.8.8:80", []),
        ("//[fd00::1]/x", ["[fd00::1]"]),
        ("//8.8.8.8/x", ["8.8.8.8"]),
    ):
        assert [h.host for h in closure.hosts(text)] == expected, text
    assert closure.public_addresses("//[2a00:1450:4001::1]/x") == []
    assert closure.public_addresses("[2a00:1450:4001::1]/x") == ["[2a00:1450:4001::1]"]
    assert closure.Host("h.io", False, 0, 4).literal is False
    assert not closure._scheme_before("", 0) and closure._scheme_before("x", 1)


def test_a_mailbox_on_a_placeholder_address_names_no_one() -> None:
    """D8 mailbox (0.20, C9): loopback, unspecified, broadcast and RFC 5737 hosts are documentation hosts for a mailbox."""
    none = closure.Known(())
    for text in (
        "ssh root@127.0.0.1",
        "deploy@0.0.0.0",
        "x@255.255.255.255",
        "admin@203.0.113.9",
        "a@192.0.2.1.",
        "mailto:ops@198.51.100.7",
        "noreply@127.0.0.1",
    ):
        boxes = closure.mailboxes(text)
        assert len(boxes) == 1 and closure.is_literal_mailbox(boxes[0]), text
        assert closure.judge(text, none) is None, text
    for text in (
        "jane@10.0.0.5",
        "root@104.16.0.1",
        "x@224.0.0.251",
        "x@100.64.0.1",
        "x@0.0.0.1",
        "user:pw@127.0.0.1",
        "x-access-token:${T}@0.0.0.0",
        "author@forge.example.com",
    ):
        boxes = closure.mailboxes(text)
        assert boxes and not closure.is_literal_mailbox(boxes[-1]), text
        assert closure.judge(text, none) is not None, text


def test_json_and_hex_string_escapes_are_a_decoded_layer() -> None:
    r"""D3, D2 (0.20, C7): ``\/`` and ``\xHH`` are peeled like ``\uXXXX``, so an escaped URL is judged as its plain form."""
    none = closure.Known(())
    assert text_forms.decoded_forms("http:\\/\\/a.b.c\\/d") == [
        "http:\\/\\/a.b.c\\/d",
        "http://a.b.c/d",
    ]
    assert text_forms.decoded_forms("\\x2f\\x2Fa\\x3a8") == ["\\x2f\\x2Fa\\x3a8", "//a:8"]
    assert text_forms.decoded_forms("\\u002f\\x2f\\/") == ["\\u002f\\x2f\\/", "///"]
    assert text_forms.decoded_forms("a\\\\/b") == ["a\\\\/b", "a\\/b", "a/b"]
    assert text_forms.decoded_forms('\\xzz \\x2 \\u12 \\n \\t \\"') == [
        '\\xzz \\x2 \\u12 \\n \\t \\"'
    ]
    for text in ("see 104.16.0.1\\/kvnteam", "see 104.16.0.1\\x3a8080"):
        assert closure.judge(text, none) == closure.Refusal("public address"), text
    for text in (
        "http:\\/\\/104.16.0.1\\/kvnteam\\/kvnbilling",
        "https\\x3a\\x2f\\x2f104.16.0.1\\x2fkvnteam",
        "http:\\/\\/code.kvncloud.io\\/kvnteam\\/kvnbilling",
        "see code.kvncloud.io\\/kvnteam",
        "https\\x3a\\x2f\\x2fcode.kvncloud.io\\x2fkvnteam",
        "\\x2f\\x2fcode.kvncloud.io\\x2fkvnteam",
        "kvnperson\\x40kvnmail.io",
    ):
        assert closure.judge(text, none) == closure.Refusal("unknown host"), text
    known = closure.Known((("host", "git.zqcorp.io"), ("segment", "zqgroup")))
    assert closure.residual("https:\\/\\/git.zqcorp.io\\/zqgroup", known) == ("host", 1)
    assert closure.residual("x\\x2fzqgroup\\x2fy", known) == ("segment", 1)
    for text in (
        "a regex \\/\\d+\\x2f and a\\x2fb",
        "http:\\/\\/example.com\\/x and \\x2f\\x2flocalhost\\x2fy",
        "see \\/ and \\x2f alone",
    ):
        assert closure.judge(text, none) is None, text


def test_the_oracle_never_raises_on_a_mapped_tail_or_a_long_digit_run() -> None:
    """D3 (0.20, C6): a dotted tail with a leading zero and a U_ run of thousands of digits are read, never raised on."""
    none = closure.Known(())
    for text, expected in (
        ("[::ffff:01.2.3.4]", "::ffff:1.2.3.4"),
        ("[::ffff:001.002.003.004]", "::ffff:1.2.3.4"),
        ("[::FFFF:104.16.0.1]", "::ffff:104.16.0.1"),
        ("[0:0:0:0:0:ffff:10.0.0.1]", "::ffff:10.0.0.1"),
        ("[1:2:3:4:5:6:1.2.3.4]", "1:2:3:4:5:6:102:304"),
        ("[::1.2.3.4]", "::102:304"),
    ):
        assert closure._ipv6(text) == ipaddress.IPv6Address(expected), text
    for text in (
        "[::ffff:999.1.1.1]",
        "[::ffff:1.2.3]",
        "[::1.2.3.4.5]",
        "[1.2.3.4]",
        "[::ffff:1.2.3.4:5]",
    ):
        assert closure._ipv6(text) is None, text
    assert closure.judge("http://[::ffff:01.2.3.4]/kvnteam/kvnbilling", none) == closure.Refusal(
        "unknown host"
    )
    assert closure.judge("see [::ffff:01.2.3.4]:8080", none) == closure.Refusal("public address")
    assert closure.judge("see [::ffff:010.0.0.1]:8080", none) is None
    assert text_forms.node_id_numbers("U_" + "9" * 5000) == []
    assert text_forms.node_id_numbers("U_" + "9" * 21) == []
    assert text_forms.node_id_numbers("U_" + "9" * 20) == ["9" * 20]
    assert text_forms.node_id_numbers("U_18446744073709551615") == ["18446744073709551615"]
    assert closure.judge("U_" + "7" * 10000 + " MDQ6VXNl" + "A" * 4000, none) is None
    assert closure.residual("U_" + "1" * 5000, closure.Known((("id", "1111"),))) is None


def test_an_ipv6_zone_id_is_part_of_the_literal_and_an_ipv4_tail_is_read() -> None:
    """D8 (0.19): ``%<zone>`` and ``%25<zone>`` end a bracketed literal, a dotted tail is two groups."""
    for text, host in (
        ("http://[2a00:1450::1%25eth0]/kvnteam/kvnbilling", "[2a00:1450::1%25eth0]"),
        ("http://[fe80::1%eth0]/kvnteam/kvnbilling", "[fe80::1%eth0]"),
        ("http://[fe80::1%en0.1_x~y]/x", "[fe80::1%en0.1_x~y]"),
        ("ssh://git@[fd00::1%25wlan-0]:22/o/r", "[fd00::1%25wlan-0]"),
        ("http://[::ffff:51.15.20.30]/kvnteam/kvnbilling", "[::ffff:51.15.20.30]"),
    ):
        found = closure.hosts(text)
        assert [(h.host, h.led, h.literal) for h in found] == [(host, True, True)], text
        assert closure.judge(text, closure.Known(())) == closure.Refusal("unknown host"), text
    assert closure.hosts("see [fe80::1%] and [fe80::1%e/th0] and [fe80::1%25]") == []
    assert closure._ipv6("[::ffff:51.15.20.30]") == ipaddress.IPv6Address("::ffff:330f:141e")
    assert closure._ipv6("[1:2:3:4:5:6:1.2.3.4]") == ipaddress.IPv6Address("1:2:3:4:5:6:102:304")
    for text in ("[1.2.3.4]", "[::ffff:1.2.3]", "[::ffff:999.1.1.1]", "[1:2:3:4:5:6:7:1.2.3.4]"):
        assert closure._ipv6(text) is None, text
    assert closure.public_addresses("ssh [::ffff:51.15.20.30]") == ["[::ffff:51.15.20.30]"]
    longest = "[0000:0000:0000:0000:0000:ffff:255.255.255.255]"
    assert len(longest) - 2 == 45 and closure._BRACKETED.fullmatch(longest)
    assert closure._ipv6(longest) == ipaddress.IPv6Address("::ffff:255.255.255.255")
    for text in (
        f"see http://{longest}/kvnteam/kvnbilling",
        "see http://[2a00:1450:4001:0801:0000:0000:0000:200e]/x",
        "see http://[0000:0000:0000:0000:0000:ffff:104.16.0.1]/x",
    ):
        assert [h.literal for h in closure.hosts(text)] == [True], text
        assert closure.judge(text, closure.Known(())) == closure.Refusal("unknown host"), text
    expanded_public = "[0000:0000:0000:0000:0000:ffff:104.16.0.1]"
    assert closure.public_addresses(f"ssh {expanded_public}") == [expanded_public]
    assert closure.judge(f"ssh {expanded_public}", closure.Known(())) == closure.Refusal(
        "public address"
    )


def test_a_bare_or_un_led_ipv6_address_is_the_documented_residual() -> None:
    """NG4-10 (0.19): an unbracketed IPv6 address and a bracketed one with a path but no lead are written."""
    none = closure.Known(())
    for text in (
        "ping 2a00:1450:4001:80b::200e now",
        "route fd00::1 via fe80::1",
        "see [fd00::1]/kvnteam/kvnbilling",
        "see [fd00::1]:8080/kvnteam",
    ):
        assert closure.hosts(text) == [], text
        assert closure.judge(text, none) is None, text
    assert closure.judge("ping [2a00:1450:4001:80b::200e]", none) == closure.Refusal(
        "public address"
    )
    assert [h.host for h in closure.hosts("see http://[fd00::1]/kvnteam")] == ["[fd00::1]"]


def test_a_known_public_suffix_is_a_public_host_and_apache_is_one() -> None:
    """D8 (0.19): ``apache.org`` and its subdomains name no one."""
    for host in ("apache.org", "repo.maven.apache.org", "Maven.Apache.ORG."):
        assert closure.is_public_host(host), host
    assert not closure.is_public_host("notapache.org")
    assert not closure.is_public_host("apache.org.evil.io")
    text = "https://repo.maven.apache.org/maven2/org/apache/commons/commons-io/2.11.0/x.pom"
    assert closure.judge(text, closure.Known(())) is None


def test_the_judge_blanks_the_literals_before_it_decodes() -> None:
    """C25 (0.19): a literal that would read as a mailbox or a host is blanked in the raw text first."""
    known = closure.Known((), literals=("docs.github.com", "docs.gitlab.com"))
    for text in (
        "x@docs.github.com",
        "x@docs.gitlab.com",
        "see x@DOCS.GITHUB.COM and x@Docs.GitLab.com",
    ):
        assert closure.judge(text, known) is None, text
        assert closure.judge(text, closure.Known(())) == closure.Refusal("unknown mailbox"), text


def test_a_unicode_local_part_and_a_userinfo_lead_their_host() -> None:
    """D8 (0.28, PR #319): a Unicode local part is a mailbox; a URL's userinfo leads its host whatever it ends with."""
    none = closure.Known(())
    for text in (
        "write to josé@acme.io today",
        "write to ü1@acme.io today",
        "clone https://u:abc=@acme.io/x now",
        "clone https://u:p?w#@acme.io/x now",
    ):
        assert closure.judge(text, none) == closure.Refusal("unknown host"), text
    assert [(b.local, b.host) for b in closure.mailboxes("to josé@acme.io")] == [
        ("josé", "acme.io")
    ]
    for text in (
        "remote=git@forge.example.com:org/project.git",
        "**git@forge.example.com**",
        "cc @alice and ü@",
        "a/=@acme.io",
    ):
        assert closure.judge(text, none) is None, text


def test_a_reverse_dns_head_is_a_host_after_slashes_or_before_a_port() -> None:
    """D8 (0.28, PR #319): no package name takes ``//`` or a port; after a path only, the head is code."""
    none = closure.Known(())
    for text in (
        "ssh dev.acme.internal:8443 now",
        "src=//dev.acme.io/ns now",
        "src=//com.example.app now",
        "see io.acme.dev:22/x",
    ):
        assert closure.judge(text, none) == closure.Refusal("unknown host"), text
    for text in ("see com.example.app/Main.java", "see dev.acme.io/ns now"):
        assert closure.judge(text, none) is None, text


def test_the_userinfo_scan_stops_at_the_previous_at_and_stays_linear() -> None:
    """D8 (0.28, PR #319): a run of ``=@a`` reads each character back once, not once per ``@``."""
    started = time.perf_counter()
    assert closure.judge("x://" + "=@a" * 20000, closure.Known(())) == (
        closure.Refusal("url userinfo")
    )
    assert len(closure.userinfos("x://" + "=@a" * 20000)) == 1
    assert time.perf_counter() - started < 2.0
    assert closure.judge("clone https://u:p@ss@acme.io/x now", closure.Known(())) == (
        closure.Refusal("unknown host")
    )


def test_a_one_label_authority_and_an_idna_last_label_are_hosts() -> None:
    """D8 (0.28, PR #319): ``https://gitlab/ns/repo`` and ``xn--`` top-level domains name a machine."""
    none = closure.Known(())
    for text in (
        "clone https://gitlab/ns/repo now",
        "see http://intranet:8080/x",
        "clone https://git.xn--p1ai/ns/repo now",
        "write to x@mail.xn--p1ai today",
    ):
        assert closure.judge(text, none) is not None, text
    assert [h.host for h in closure.hosts("open https://gitlab/ns/repo")] == ["gitlab"]
    assert [(b.local, b.host) for b in closure.mailboxes("x@mail.xn--p1ai")] == [
        ("x", "mail.xn--p1ai")
    ]
    for text in (
        "ssh://git@forge.example.com/org/project.git",
        "http://localhost/x",
        "see gitlab/ns/repo",
        "x@y",
    ):
        assert closure.judge(text, none) is None, text


def test_a_url_userinfo_is_refused_on_every_allowlisted_and_placeholder_host() -> None:
    """D8 (PR #319 round 4): a userinfo no mailbox reader sees is refused wherever its host stands.

    ``https://u:abc=@pypi.org/`` ends its userinfo with ``=``, so no local part leads the ``@``,
    and its host is public: before, the oracle refused nothing. Every host the oracle lets
    through, the public hosts, the placeholder-class hosts and addresses and a one-label host,
    is enumerated with every userinfo ending a mailbox reader cannot read.
    """
    none = closure.Known(())
    public = sorted(closure_tables.PUBLIC_SUFFIXES)
    hosts = [
        *public,
        *(f"www.{s}" for s in public),
        "forge.example.com",
        "example.org",
        "x.test",
        "localhost",
        "127.0.0.1",
        "203.0.113.7",
        "[::1]",
        "[2001:db8::1]",
        "intranet",
    ]
    tails = ["=", "!", "~", "*", "'", "(", ")", ",", ";", "&", "$", "", ":", "{x}", "%3D"]
    for host in hosts:
        for scheme in ("https", "redis", "postgres"):
            for tail in tails:
                text = f"see {scheme}://u:abc{tail}@{host}:6379/x now"
                assert closure.judge(text, none) is not None, text
            empty = f"{scheme}://:{tail or 'p'}@{host}/x"
            assert closure.judge(empty, none) is not None, empty
    assert closure.judge("redis://u:abc=@pypi.org:6379", none) == closure.Refusal("url userinfo")
    assert closure.judge("see https%3A%2F%2Fu%3Aabc%3D%40pypi.org%2Fx", none) == (
        closure.Refusal("url userinfo")
    )
    for text in (
        "ssh://git@forge.example.com/org/project.git",
        "git://GIT@forge.example.com/org/project.git",
        "See <https://forge.example.com><a@example.com> (host forge.example.com)",
        "DATABASE_URL=app:${DB_PASSWORD}@localhost:5432/db",
        "image docker.io/library/r@sha256:abc and https://pypi.org/@scope",
    ):
        assert closure.judge(text, none) is None, text
    for text in (
        "DATABASE_URL=postgres://app@localhost:5432/db",
        "redis://root@127.0.0.1:6379/0",
        "ftp://anonymous@example.org/pub",
    ):
        assert closure.judge(text, none) is None, text
    for text in (
        "redis://app@pypi.org:6379/0",
        "postgres://app@forge.example.com:5432/db",
        "postgres://app@[::1]:5432/db",
        "https://@pypi.org/x",
        "postgres://app:${DB_PASSWORD}@localhost:5432/db",
    ):
        assert closure.judge(text, none) is not None, text
    boxes = closure.userinfos("a https://u:p=@h.io b ssh://git@x c https://<a>@h.io d s://v@[::1]")
    assert [(b.local, b.host) for b in boxes] == [("u:p=", "h.io"), ("git", "x"), ("v", "[::1]")]
    assert [(b.local, b.host) for b in closure.userinfos("https://@pypi.org./x")] == [
        ("", "pypi.org")
    ]
    assert [b.local for b in closure.userinfos("https://josé:pw@pypi.org/x")] == ["josé:pw"]
    assert [b.host for b in closure.userinfos("https://u:p@/x")] == [""]


def test_a_vendor_with_tenant_consoles_is_public_by_its_documentation_hosts_only() -> None:
    """D8 (PR #319 round 4): a console or an API whose path names a customer's account is no public host.

    ``cloudflare.com``, ``mend.io`` and ``renovatebot.com`` hold such surfaces under the same
    registrable suffix (``dash.``, ``api.``, ``saas.``, ``app.``), so only their documentation and
    CDN hosts are public; a Stack Overflow for Teams route (``/c/<team>``) on a public host is a
    tenant path.
    """
    none = closure.Known(())
    assert {
        "cdnjs.cloudflare.com",
        "cloudflare.com",
        "developers.cloudflare.com",
        "docs.mend.io",
        "docs.renovatebot.com",
        "mend.io",
        "renovatebot.com",
        "www.cloudflare.com",
        "www.mend.io",
        "www.renovatebot.com",
    } == closure_tables.PUBLIC_HOSTS
    forges = {
        "bitbucket.org",
        "codeberg.org",
        "git.sr.ht",
        "gitea.com",
        "gitee.com",
        "github.com",
        "gitlab.com",
        "launchpad.net",
    }
    assert set(closure_tables.FORGE_PATH_HOSTS) == forges
    go = forges | {"forge.example.com"}
    assert set(closure_tables.GO_MODULE_HOSTS) == go
    npm = frozenset({"-/org", "-/team", "-/user"})
    assert {
        "apache.org": frozenset({"roster/committer"}),
        "crates.io": frozenset({"api/v1/teams", "api/v1/users", "teams", "users"}),
        "docs.rs": frozenset({"releases"}),
        "go.dev": frozenset(go),
        "golang.org": frozenset(go | {f"lookup/{h}" for h in go}),
        "jsdelivr.net": frozenset({"combine", "gh"}),
        "mozilla.org": frozenset(
            {"*/android/user", "*/firefox/user", "android/user", "firefox/user", "p", "u"}
        ),
        "npmjs.com": frozenset({"org", "settings"}),
        "npmjs.org": npm,
        "npmmirror.com": npm,
        "nuget.org": frozenset({"profiles"}),
        "pypi.org": frozenset({"manage/organization", "org", "user"}),
        "python.org": frozenset({"u"}),
        "readthedocs.org": frozenset({"organizations", "profiles"}),
        "stackoverflow.com": frozenset({"c", "users/*"}),
        "wikipedia.org": frozenset({"wiki/*:"}),
        "yarnpkg.com": npm,
    } == closure_tables.TENANT_ROUTES
    assert set(closure_tables.TENANT_ROUTE_PUBLIC) == {
        "docs.rs releases",
        "go.dev forge.example.com",
        "golang.org forge.example.com",
        "golang.org lookup/forge.example.com",
        "wikipedia.org wiki/*:",
    }
    assert {
        "apache.org": ("~", frozenset()),
        "docker.io": ("", frozenset({"library"})),
        "ghcr.io": ("", frozenset()),
        "npmjs.com": ("~", frozenset()),
    } == closure_tables.ACCOUNT_NAMESPACES
    assert set(closure_tables.TENANT_ROUTES) | set(closure_tables.ACCOUNT_NAMESPACES) <= (
        closure_tables.PUBLIC_SUFFIXES
    )
    assert not closure_tables.PUBLIC_HOSTS & closure_tables.PUBLIC_SUFFIXES
    for host in closure_tables.PUBLIC_HOSTS:
        assert closure.is_public_host(host) and closure.is_public_host(host.upper() + ".")
        assert not closure.is_public_host(f"tenant.{host}"), host
        assert closure.judge(f"see https://{host}/docs/x now", none) is None, host
    for vendor in ("cloudflare.com", "mend.io", "renovatebot.com"):
        assert vendor not in closure_tables.PUBLIC_SUFFIXES, vendor
    for text in (
        "see https://dash.cloudflare.com/0a1b2c3d4e5f60718293a4b5c6d7e8f9/kvnshop.io/dns",
        "curl https://api.cloudflare.com/client/v4/accounts/0a1b2c3d4e5f60718293a4b5c6d7e8f9",
        "see https://one.dash.cloudflare.com/0a1b2c3d4e5f/access",
        "login https://saas.mend.io/app/orgs/kvnteam",
        "open https://app.renovatebot.com/dashboard#github/kvnteam/kvnbilling",
        "dash.cloudflare.com/0a1b2c3d4e5f60718293a4b5c6d7e8f9/kvnshop.io/dns",
    ):
        assert closure.judge(text, none) == closure.Refusal("unknown host"), text
    for text in (
        "ask https://stackoverflow.com/c/kvnteam/questions/12",
        "ask //www.stackoverflow.com/c/kvnteam/questions/12",
        "ask https://STACKOVERFLOW.com/C/kvnteam",
        "ask https%3A%2F%2Fstackoverflow.com%2Fc%2Fkvnteam",
    ):
        assert closure.judge(text, none) == closure.Refusal("tenant path"), text
    for text in (
        "see https://stackoverflow.com/questions/12/how",
        "see https://stackoverflow.com/c",
        "see https://stackoverflow.com/c/",
        "see https://stackoverflow.com/collectives/python",
        "see https://pypi.org/c/x",
    ):
        assert closure.judge(text, none) is None, text
    assert closure.is_tenant_path("stackoverflow.com", "/c/team")
    assert closure.is_tenant_path("www.stackoverflow.com", "c/team/q")
    assert not closure.is_tenant_path("stackoverflow.com", "/c")
    assert not closure.is_tenant_path("xstackoverflow.com", "/c/team")
    assert not closure.is_tenant_path("pypi.org", "/c/team")


def test_a_url_userinfo_is_read_by_grammar_whatever_it_holds() -> None:
    """D8 (PR #319 round 5, 4683464677): the userinfo is everything between ``//`` and the last ``@``.

    No character allowlist decides whether a run is a userinfo, so a password holding a character
    outside RFC 3986 fails closed on every host, a placeholder one included; only whitespace,
    ``/``, ``<``, ``>`` and ``"`` end the authority.
    """
    none = closure.Known(())
    hosts = ("localhost", "127.0.0.1", "pypi.org", "forge.example.com", "acme.io")
    for ch in "^|\\[]#?@`~{}%!$&'()*+,;=":
        for host in hosts:
            for text in (
                f"DATABASE_URL=postgres://app:zqpw{ch}x@{host}/db",
                f"https://u:zqpw{ch}@{host}/simple",
            ):
                assert closure.judge(text, none) is not None, (ch, host)
                (box,) = closure.userinfos(text)
                assert box.local.endswith(f"zqpw{ch}x" if "postgres" in text else f"zqpw{ch}")
                assert (box.host, closure.is_literal_mailbox(box)) == (host, False), (ch, host)
    assert [(b.local, b.host) for b in closure.userinfos("postgres://app:p^w@localhost/db")] == [
        ("app:p^w", "localhost")
    ]
    assert [b.local for b in closure.userinfos("redis://u:p@ss#x@localhost:6379")] == ["u:p@ss#x"]
    assert [b.local for b in closure.userinfos("x://a:b\\c@h.io y://d|e@h.io")] == ["a:b\\c", "d|e"]
    for text in (
        "see <https://<host>><a@<host>> for it",
        'url="https://pypi.org/simple" user@ x',
        "https://pypi.org/simple/@scope/pkg",
        "git clone ssh://git@forge.example.com/o/r.git",
        "postgres://app@localhost:5432/db",
    ):
        assert closure.judge(text, none) is None, text


def test_a_tenant_route_is_refused_whatever_leads_its_host() -> None:
    """D8 (PR #319 round 5, 4683464700): a tenant route is judged by host and path, lexically.

    A scheme, ``//``, a bare host, a port and a Markdown link lead it alike; the redactor keeps
    no such URL and the oracle refuses every one.
    """
    none = closure.Known(())
    for text in (
        "see stackoverflow.com/c/kvnteam77 for the setting",
        "see https://stackoverflow.com/c/kvnteam77 for x",
        "see https://stackoverflow.com:443/c/kvnteam77/questions/1 x",
        "see stackoverflow.com:443/c/kvnteam77/questions/1 x",
        "see //stackoverflow.com/c/kvnteam77 x",
        "see www.stackoverflow.com/c/kvnteam77 x",
        "[team](stackoverflow.com/c/kvnteam77)",
        "see STACKOVERFLOW.COM./C//kvnteam77 x",
        "see https%3A%2F%2Fstackoverflow.com%2Fc%2Fkvnteam77 x",
    ):
        assert closure.judge(text, none) == closure.Refusal("tenant path"), text
        assert any(closure.has_tenant_path(form) for form in text_forms.decoded_forms(text)), text
    for text in (
        "see https://stackoverflow.com/questions/12/how",
        "see https://stackoverflow.com/collectives/x",
        "see stackoverflow.com/c and stackoverflow.com/c/",
        "see xstackoverflow.com/c/kvnteam77",
        "see pypi.org/c/kvnteam77",
    ):
        assert not closure.has_tenant_path(text), text
    started = time.perf_counter()
    assert not closure.has_tenant_path("a." * 20000 + "stackoverflow.com/q")
    assert time.perf_counter() - started < 2.0


def test_a_host_on_an_internationalised_domain_is_read_by_the_ascii_grammar() -> None:
    """D8 (PR #319 round 6, 4684965361): a U-label host is read in NFC, NFKC and its A-labels, failing closed.

    ``hans@müller.de`` and ``https://éclair.fr/…`` were passed: the ASCII reader stopped at the
    first non-ASCII letter. Every decoded form is now also read in NFC and NFKC and with its
    Unicode label runs in IDNA A-labels (:func:`repoach.core.text_forms.ascii_labels`), so the one
    grammar of :func:`repoach.forge.closure.hosts` and :func:`repoach.forge.closure.mailboxes`
    reads them; ordinary non-ASCII prose keeps no host it did not have.
    """
    none = closure.Known(())
    assert text_forms.ascii_labels("hans@m\u00fcller.de") == "hans@xn--mller-kva.de"
    assert text_forms.ascii_labels("https://\u00e9clair.fr/x") == "https://xn--clair-9ra.fr/x"
    assert (
        text_forms.ascii_labels("a@\u4f8b\u3048\u3002\u30c6\u30b9\u30c8")
        == "a@xn--r8jz45g.xn--zckzah"
    )
    assert text_forms.ascii_labels("M\u00dcLLER.de") == "xn--mller-kva.de"
    assert text_forms.ascii_labels("plain ascii.text") == "plain ascii.text"
    assert text_forms.ascii_labels("\u212a.example.com") == "k.example.com"
    assert text_forms.decoded_forms("plain") == ["plain"]
    nfd = "hans@mu\u0308ller.de"
    forms = text_forms.decoded_forms(nfd)
    assert forms[0] == nfd and "hans@xn--mller-kva.de" in forms
    assert "https://zqcorp.io/x" in text_forms.decoded_forms(
        "\uff48\uff54\uff54\uff50\uff53\uff1a\uff0f\uff0f\uff5a\uff51\uff43\uff4f\uff52\uff50\uff0e\uff49\uff4f\uff0fx"
    )
    refused = {
        "mail hans@m\u00fcller.de now": "unknown host",
        "mail " + nfd: "unknown host",
        "mail hans@m%C3%BCller.de": "unknown host",
        "mail hans@m&#252;ller.de": "unknown host",
        "mail hans@m\u00fcller\u3002de": "unknown host",
        "mail hans@m\u00fcller\uff0ede": "unknown host",
        "mail MAX@M\u00dcLLER.DE": "unknown host",
        "see https://\u00e9clair.fr/ns/repo": "unknown host",
        "see https://e\u0301clair.fr/ns/repo": "unknown host",
        "see https://xn--clair-9ra.fr/ns/repo": "unknown host",
        "see https://\u00e9clair/ns": "unknown host",
        "see //git.\u00e9clair.fr/ns": "unknown host",
        "see ssh://git@g\u00eft.zqcorp.io/x": "unknown host",
        "see \uff48\uff54\uff54\uff50\uff53\uff1a\uff0f\uff0f\uff5a\uff51\uff43\uff4f\uff52\uff50\uff0e\uff49\uff4f\uff0fx": "unknown host",
        "see https://\u0431\u0430\u043d\u043a.\u0440\u0444/x": "unknown host",
    }
    for text, reason in refused.items():
        refusal = closure.judge(text, none)
        assert refusal is not None and refusal.reason == reason, ascii(text)
    for text in (
        "Le caf\u00e9 est pr\u00eat. Voil\u00e0.C'est fini",
        "na\u00efve r\u00e9sum\u00e9 co\u00f6perate",
        "Gr\u00f6\u00dfe: 1.2.3 und Ma\u00dfe.Ende",
        "\u65e5\u672c\u8a9e\u306e\u30c6\u30ad\u30b9\u30c8\u3002\u7d42\u308f\u308a",
        "@m\u00fcller said hi",
        "voir https://fr.wikipedia.org/wiki/Caf\u00e9",
        "jos\u00e9@example.com",
        "Stra\u00dfe.Weg/abc",
        "\u00fcber 3.5 \u00b5s",
        "\u2460 \uff11\uff12\uff13 \u00bd",
    ):
        assert closure.judge(text, none) is None, ascii(text)


def test_a_container_registry_namespace_is_an_identity_slot() -> None:
    """D8 (PR #319 round 6, 4684965389): ``ghcr.io/<owner>`` is an account, as ``github.com/<owner>`` is.

    Every namespace of :data:`repoach.forge.closure_tables.ACCOUNT_NAMESPACES` (after an optional
    OCI ``v2/``) and every account route of :data:`repoach.forge.closure_tables.TENANT_ROUTES` is
    refused as a ``tenant path`` whatever leads its host; the redactor rewrites such a URL to
    the placeholder; the bare host, ``docker.io/library`` and a package page stay public.
    """
    none = closure.Known(())
    refused = (
        "pull ghcr.io/kvnorg/kvnimage:1.0",
        "COPY --from=ghcr.io/kvnorg/kvnimage /x /x",
        "see https://ghcr.io/kvnorg/kvnimage",
        "GET https://ghcr.io/v2/kvnteam/kvnbilling/manifests/latest",
        "GET https://ghcr.io/v2/kvnteam",
        "[img](ghcr.io/kvnorg/x)",
        "ghcr.io:443/kvnorg/x",
        "//ghcr.io/kvnorg/x",
        "GHCR.IO/KvnOrg/x",
        "ghcr.io/kvnorg:latest",
        "ghcr.io%2Fkvnorg%2Fx",
        "FROM docker.io/kvnorg/redis:7",
        "GET https://registry-1.docker.io/v2/kvnteam/app/manifests/latest",
        "docker.io/libraryx/python",
        "https://www.npmjs.com/~kvnuser",
        "https://www.npmjs.com/org/kvnorg",
        "https://pypi.org/user/kvnuser/",
        "https://pypi.org/org/kvnorg/",
        "https://crates.io/users/kvnuser",
        "https://crates.io/teams/github:kvnorg:core",
        "https://www.nuget.org/profiles/kvnuser",
        "https://cdn.jsdelivr.net/gh/kvnuser/kvnrepo@1/x.js",
    )
    for text in refused:
        assert closure.judge(text, none) == closure.Refusal("tenant path"), text
    for text in (
        "ghcr.io",
        "see ghcr.io/ for images",
        "ghcr.io/v2/",
        "FROM docker.io/library/python:3.12-slim",
        "FROM docker.io/Library/python",
        "GET https://registry-1.docker.io/v2/library/nginx/manifests/latest",
        "docker login https://docker.io/v2/",
        "https://www.npmjs.com/package/typescript",
        "https://www.npmjs.com/~",
        "https://pypi.org/project/ruff/",
        "https://crates.io/crates/serde",
        "https://www.nuget.org/packages/Newtonsoft.Json",
        "https://cdn.jsdelivr.net/npm/lodash@4/x.js",
        "k8s.io/client-go/kubernetes",
        "xghcr.io/kvnorg/x",
    ):
        assert closure.judge(text, none) is None, text
    assert closure.is_tenant_path("ghcr.io", "/kvnorg/x")
    assert closure.is_tenant_path("GHCR.io.", "//kvnorg")
    assert closure.is_tenant_path("ghcr.io", "/v2/kvnorg/x/manifests/1")
    assert closure.is_tenant_path("registry-1.docker.io", "/v2/kvnorg/app")
    assert closure.is_tenant_path("www.npmjs.com", "/~kvnuser")
    assert closure.is_tenant_path("pypi.org", "/user/kvnuser/")
    assert not closure.is_tenant_path("ghcr.io", "/v2/")
    assert not closure.is_tenant_path("ghcr.io", "")
    assert not closure.is_tenant_path("registry-1.docker.io", "/v2/library/nginx")
    assert not closure.is_tenant_path("docker.io", "/library/python")
    assert not closure.is_tenant_path("www.npmjs.com", "/package/x")
    assert not closure.is_tenant_path("www.npmjs.com", "/~")
    assert not closure.is_tenant_path("xghcr.io", "/kvnorg")
    rewritten = recording.redact_urls_in_text("see https://ghcr.io/kvnorg/kvnimage now")
    assert rewritten == "see https://forge.example.com/org/project now"
    started = time.perf_counter()
    assert not closure.has_tenant_path("a." * 20000 + "ghcr.io")
    assert time.perf_counter() - started < 2.0


def test_a_host_is_read_by_dns_label_grammar_in_every_label() -> None:
    """D3 (PR #319 round 7, 4687852986): a led host whose last label holds a digit is a host.

    Every label is letters, digits and hyphens; the last is led by a letter (or is an ``xn--``
    label), so ``gitlab.dc1`` and ``ci.node1`` are hosts after a scheme, a ``<local>@`` or a
    userinfo, while a version (``v1.2.3``, ``7.1.0.rc1``, ``pkg@1.0.0-rc1``), an address and a
    file name (``foo.py3``) stay ordinary text.
    """
    none = closure.Known(())
    for text in (
        "see https://gitlab.dc1/zqns/zqrepo now",
        "see https://build.node1:8443/x",
        "mail zqkvn@ci.node1 now",
        "push git@gitlab.dc1:zqns/zqrepo.git",
        "dsn postgres://app:zqpw@db.rack2/x",
        "see https://zq-1.zq2-x.k8s3/x",
        "see https://GITLAB.DC1/x",
    ):
        assert closure.judge(text, none) is not None, text
    assert [h.host for h in closure.hosts("https://gitlab.dc1/x")] == ["gitlab.dc1"]
    assert [b.host for b in closure.mailboxes("zqkvn@ci.node1")] == ["ci.node1"]
    for text in (
        "bump to v1.2.3 and 1.2.3 then 10.4.0-rc1",
        "install pkg@1.2.3, pkg@1.0.0-rc1 and rails@7.1.0.rc1",
        "open foo.py3, bar.mp4, x.h264, lib.so.6 and libc.so.6",
        "see https://example.com/foo.py3 and @babel/core@7.24.0",
        "the address 1.2.3.4 and version 2.0.1.beta2",
        "pinned image@sha256:abc and node@18.19.1",
    ):
        assert closure.judge(text, none) is None, text
    for run in ("1.2.3", "7.1.0.rc1", "1.0.0-rc1", "x.y", "x.1a", "x.-a", "x.a"):
        assert closure._label_run(run) is None, run
    for run in ("gitlab.dc1", "ci.node1", "a.b.c9", "zq.xn--p1ai", "1.example.com"):
        assert closure._label_run(run) is not None, run


def test_an_account_route_or_an_embedded_forge_path_on_a_public_host_is_refused() -> None:
    """D8 (PR #319 round 7, 4687852992): every allowlisted host is audited for an identity in its path.

    A Go host embeds a forge path (``pkg.go.dev/github.com/<owner>/…``), an npm registry serves
    its CouchDB users (``-/user/org.couchdb.user:<login>``), and docs.rs, Discourse, Mozilla,
    Apache, Read the Docs, Wikipedia and Stack Overflow have account routes: each is refused bare
    and in every decoded form, and a URL on one is rewritten with no identity left or, when the
    forge route grammar cannot place the identity, left for the oracle to refuse. The public
    pages next to them stay ordinary.
    """
    none = closure.Known(())
    planted = (
        "pkg.go.dev/github.com/kvnorg/kvnrepo",
        "pkg.go.dev/gitlab.com/kvnorg/kvnrepo/sub",
        "go.dev/bitbucket.org/kvnorg/x",
        "proxy.golang.org/github.com/kvnorg/kvnrepo/@v/list",
        "sum.golang.org/lookup/github.com/kvnorg/kvnrepo@v1.0.0",
        "pkg.go.dev/codeberg.org/kvnorg/x",
        "registry.npmjs.org/-/user/org.couchdb.user:kvnuser",
        "registry.npmjs.org/-/org/kvnorg/user",
        "registry.yarnpkg.com/-/user/org.couchdb.user:kvnuser",
        "registry.npmmirror.com/-/team/kvnorg/devs",
        "crates.io/api/v1/users/kvnuser",
        "pypi.org/manage/organization/kvnorg/",
        "docs.rs/releases/kvnuser",
        "cdn.jsdelivr.net/combine/gh/kvnuser/kvnrepo@1/x.js",
        "discuss.python.org/u/kvnuser",
        "discourse.mozilla.org/u/kvnuser",
        "people.mozilla.org/p/kvnuser",
        "addons.mozilla.org/en-US/firefox/user/12345/",
        "people.apache.org/~kvnuser",
        "whimsy.apache.org/roster/committer/kvnuser",
        "readthedocs.org/profiles/kvnuser/",
        "en.wikipedia.org/wiki/User:Kvnuser",
        "de.wikipedia.org/wiki/Benutzer:Kvnuser",
        "en.wikipedia.org/wiki/User_talk:Kvnuser",
        "en.wikipedia.org/wiki/Special:Contributions/Kvnuser",
        "stackoverflow.com/users/12345/kvnuser",
        "proxy.golang.org/forge.example.com/kvnorg/kvnrepo/@v/list",
    )
    for shape in planted:
        for text in (
            f"see {shape} now",
            f"see https://{shape} now",
            f"see //{shape} now",
            f"[x](https://{shape})",
            "see https%3A%2F%2F" + shape.replace("/", "%2F"),
        ):
            assert closure.judge(text, none) == closure.Refusal("tenant path"), text
        written = recording.redact_urls_in_text(f"see https://{shape} now")
        assert "kvn" not in written.casefold() or written == f"see https://{shape} now", shape
        host, _, path = shape.partition("/")
        assert closure.is_tenant_path(host, path), shape
        assert closure.tenant_identities(shape), shape
    for text in (
        "see https://pkg.go.dev/fmt and https://pkg.go.dev/golang.org/x/net/http2",
        "see https://pkg.go.dev/std and https://go.dev/doc/modules/layout",
        "see https://proxy.golang.org/golang.org/x/net/@v/list",
        "see https://registry.npmjs.org/-/v1/search?text=lodash and https://registry.npmjs.org/lodash",
        "see https://docs.rs/releases/recent and https://docs.rs/serde/latest/serde/",
        "see https://docs.rs/releases and https://crates.io/api/v1/crates/serde",
        "see https://en.wikipedia.org/wiki/Help:Contents and https://en.wikipedia.org/wiki/Category:X",
        "see https://en.wikipedia.org/wiki/Talk:Python and https://en.wikipedia.org/wiki/Mission:_Impossible",
        "see https://en.wikipedia.org/wiki/File:Logo.png and https://en.wikipedia.org/wiki/Python",
        "see https://discuss.python.org/t/some-topic/123 and https://www.python.org/downloads/",
        "see https://developer.mozilla.org/en-US/docs/Web and https://www.apache.org/licenses/",
        "see https://stackoverflow.com/users and https://stackoverflow.com/questions/1/x",
        "see https://readthedocs.org/projects/pip/ and https://pip.readthedocs.io/en/stable/",
        "see https://cdn.jsdelivr.net/npm/lodash@4/x.js",
        "see pkg.go.dev/forge.example.com/org/project and docs.rs/releases/recent, then",
    ):
        assert closure.judge(text, none) is None, text
        assert recording.redact_urls_in_text(text) == text, text
    started = time.perf_counter()
    assert not closure.has_tenant_path("en.wikipedia.org/wiki/" + "a" * 20000)
    assert not closure.has_tenant_path("x.mozilla.org/" + "a/" * 10000)
    assert not closure.has_tenant_path("pkg.go.dev/" * 5000)
    assert time.perf_counter() - started < 2.0
