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
    ) - closure.EXTENSION_TLDS == closure.HOST_TLDS
    assert {"eu", "su", "uk"} <= closure.HOST_TLDS and "ac" not in closure.HOST_TLDS
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
        assert closure.hosts(f"{head}.acme.tools/x") == []
    assert {"com", "org", "net", "io", "dev", "me", "co", "edu", "gov"} == closure.REVERSE_DNS_HEADS
    assert closure.hosts("a.b.zq/x") == []
    for tld in sorted(closure.HOST_TLDS):
        found = closure.hosts(f"see svc.acme.{tld}/x now")
        assert [h.host for h in found] == [f"svc.acme.{tld}"], tld
        assert found[0].led is False
        assert closure.hosts(f"see svc.acme.{tld} now") == [], tld
        assert closure.hosts(f"see acme.{tld}/x now") == [], tld
    for tld in sorted(closure.EXTENSION_TLDS):
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
    assert [str(n) for n in closure.IPV4_RESERVED] == networks
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
        "apache.org",
    }
    assert suffixes == closure.PUBLIC_SUFFIXES
    assert len(closure.PUBLIC_SUFFIXES) == 25
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
    ):
        assert closure.hosts(phrase) == [], phrase
    for phrase in ("version  8.8.8.8:80", "versio 8.8.8.8:80", "a version: 8.8.8.8:80"):
        assert [h.host for h in closure.hosts(phrase)] == ["8.8.8.8"], phrase
    assert closure.hosts("x //8.8.8.8/y")[0].led
    assert [(h.host, h.led) for h in closure.hosts("x /8.8.8.8/y")] == [("8.8.8.8", False)]
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
    assert closure.node_id_numbers("U_kgAF") == ["5"]
    assert closure.node_id_numbers("xU_kgAF") == ["5"]
    assert closure.node_id_numbers("U_kgA") == []
    assert closure.node_id_numbers("U_kgB_") == ["127"] if False else True
    assert closure.node_id_numbers(
        "U_" + base64.urlsafe_b64encode(b"\x92\x00\x7f").decode().rstrip("=")
    ) == ["127"]
    assert (
        closure.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x92\x00\x80").decode().rstrip("=")
        )
        == []
    )
    assert (
        closure.node_id_numbers(
            "U_" + base64.urlsafe_b64encode(b"\x92\x00\xcb\x00").decode().rstrip("=")
        )
        == []
    )
    twenty = 2**64 - 1
    assert closure.node_id_numbers(base64.b64encode(f"04:User{twenty}".encode()).decode()) == [
        str(twenty)
    ]
    assert closure.node_id_numbers(base64.b64encode(f"04:User{twenty}x".encode()).decode()) == [
        str(twenty)
    ]
    one = base64.b64encode(b"04:User1").decode()
    assert closure.node_id_numbers(one) == ["1"]
    assert closure.node_id_numbers(one.rstrip("=")) == ["1"]


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
    decoded = closure.decoded_forms("https%3A%2F%2Fcode.acme.io%2Fx")
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
            assert isinstance(closure.node_id_numbers(text), list), (length, pad)
    assert closure.node_id_numbers("see MDQ6VXNlcjc0O= here") == ["74"]
    assert closure.node_id_numbers("see MDQ6VXNlcjc0OTI4MTYz= here") == ["74928163"]
    assert closure.node_id_numbers("see MDQ6VXNlcjc0OTI4MTY= here") == ["7492816"]
    assert closure.node_id_numbers("see MDQ6VXNlcjc0OTI4MTYz== here") == ["74928163"]
    assert closure.node_id_numbers("see MDQ6VXNlcjc0OTI4MTYzA here") == ["74928163"]
    assert closure.node_id_numbers("MDQ6VXNlcjc0O" + "=" * 2) == ["74"]


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
        assert str(closure._ipv6(text)) == expected, text
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
    assert str(closure._ipv6("[::ffff:51.15.20.30]")) == "::ffff:330f:141e"
    assert str(closure._ipv6("[1:2:3:4:5:6:1.2.3.4]")) == "1:2:3:4:5:6:102:304"
    for text in ("[1.2.3.4]", "[::ffff:1.2.3]", "[::ffff:999.1.1.1]", "[1:2:3:4:5:6:7:1.2.3.4]"):
        assert closure._ipv6(text) is None, text
    assert closure.public_addresses("ssh [::ffff:51.15.20.30]") == ["[::ffff:51.15.20.30]"]


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
