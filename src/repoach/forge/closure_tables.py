"""The data tables of the closure oracle: top-level domains, reserved networks and public hosts.

SP-RECORDER-REDACTS-BY-CLASS D8. :mod:`repoach.forge.closure` judges a
built fixture with explicit sets and one short reader each; the sets live
here, so the readers stay reviewable on their own. Like the oracle, this
module imports the standard library only and shares no set with
``repoach.forge.recording``.
"""

from __future__ import annotations

import ipaddress

URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "ssh", "git", "ftp", "ftps"})
"""Schemes the convenience layer rewrites; any other ``<scheme>://`` leads a host as well."""

_ISO_COUNTRY_CODES = [
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

_RESERVED_COUNTRY_CODES = ["ac", "eu", "su", "uk"]
"""Country-level codes outside ISO 3166-1 (exceptional reservations and retired codes) that name hosts."""

_GENERIC_TLDS = [
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
    "cloud",
    "tech",
    "site",
    "online",
    "xyz",
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

_PRIVATE_USE_TLDS = [
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

EXTENSION_TLDS: frozenset[str] = frozenset(
    {
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
    }
)
"""Country and generic codes that are also the extension of a source, build or binary file.

A compiler or a linter cites a three-label file name as ``<file>:<line>``
(``settings.local.py:10``, ``config.h.in:5``, ``kernel/foo.bar.cu:12:3``,
``core.alu.sv:33``), the one follow shape a file name shares with a host;
un-led, such a name is a file, not a host. The class: autoconf and
automake (``ac``, ``am``, ``in``, ``mk``), C++ and CUDA (``cc``, ``cu``),
Crystal (``cr``), GDScript (``gd``), Markdown (``md``), OCaml (``ml``),
Objective-C++ (``mm``), Perl (``pl``, ``pm``), Qt and Prolog (``pro``),
PostScript (``ps``), Python (``py``), Rust (``rs``), Scala scripts
(``sc``), shell (``sh``), shared objects (``so``), Smalltalk (``st``),
SystemVerilog (``sv``), Terraform (``tf``).
"""

HOST_TLDS: frozenset[str] = (
    frozenset(_ISO_COUNTRY_CODES)
    | frozenset(_RESERVED_COUNTRY_CODES)
    | frozenset(_GENERIC_TLDS)
    | frozenset(_PRIVATE_USE_TLDS)
) - EXTENSION_TLDS
"""The explicit list of last labels that make a run of three labels a host."""

REVERSE_DNS_HEADS: frozenset[str] = frozenset(
    {"com", "org", "net", "io", "dev", "me", "co", "edu", "gov"}
)
"""First labels of Java, Android and Maven names (``com.example.app``).

A name with such a head is never a host when only a ``/<segment>`` follows it (a source path,
``com/example/app``'s dotted form); led by ``//`` or followed by ``:<port digits>`` it is a host
like any other, since no package name takes a port or a network-path lead.
"""

IPV4_RESERVED = tuple(
    ipaddress.IPv4Network(n)
    for n in (
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
    )
)
"""Private, loopback, documentation and reserved networks: an address in one is not public."""

RFC5737_NETWORKS = tuple(
    ipaddress.IPv4Network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
"""The IPv4 documentation networks (RFC 5737): a host in one names no one."""

UNSPECIFIED_BROADCAST = (
    ipaddress.IPv4Address("0.0.0.0"),
    ipaddress.IPv4Address("255.255.255.255"),
)
"""The unspecified and the limited-broadcast address (RFC 1122): a bind or a log line, never a machine."""

IPV6_DOCUMENTATION = tuple(ipaddress.IPv6Network(n) for n in ("2001:db8::/32", "3fff::/20"))
"""The IPv6 documentation prefixes (RFC 3849, RFC 9637): a literal in one names no one."""

PUBLIC_SUFFIXES: frozenset[str] = frozenset(
    {
        "apache.org",
        "crates.io",
        "docker.io",
        "docs.github.com",
        "docs.gitlab.com",
        "docs.rs",
        "ghcr.io",
        "go.dev",
        "golang.org",
        "jsdelivr.net",
        "k8s.io",
        "mozilla.org",
        "nuget.org",
        "npmjs.com",
        "npmjs.org",
        "npmmirror.com",
        "pypi.org",
        "pythonhosted.org",
        "python.org",
        "readthedocs.io",
        "readthedocs.org",
        "stackoverflow.com",
        "unpkg.com",
        "wikipedia.org",
        "yarnpkg.com",
    }
)
"""Registrable suffixes of public documentation, package and CDN hosts (option A; 0.22: the yarn and npmmirror registries).

A suffix is listed only when no host under it is a tenant surface: a console or an API whose
path names a customer's account (PR #319 round 4 audit). A vendor that runs one under the same
suffix is listed by its documentation and CDN hosts instead (:data:`PUBLIC_HOSTS`), and a
tenant route on a listed host is :data:`TENANT_ROUTES`. Audited, every suffix here: the
registries (``crates.io``, ``docker.io``, ``ghcr.io``, ``npmjs.com``, ``npmjs.org``,
``npmmirror.com``, ``nuget.org``, ``pypi.org``, ``pythonhosted.org``, ``yarnpkg.com``,
``unpkg.com``, ``jsdelivr.net``, ``k8s.io``) name a public package, image or account in their
paths; the documentation sites (``docs.github.com``, ``docs.gitlab.com``, ``docs.rs``,
``go.dev``, ``golang.org``, ``python.org``, ``mozilla.org``, ``apache.org``,
``readthedocs.io``, ``readthedocs.org``, ``wikipedia.org``) and ``stackoverflow.com`` serve
public pages, a project's documentation subdomain included.
"""

PUBLIC_HOSTS: frozenset[str] = frozenset(
    {
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
    }
)
"""Exact public hosts of vendors whose registrable suffix also holds tenant surfaces (PR #319 round 4).

``cloudflare.com`` holds the dashboard and the API (``dash.cloudflare.com/<account>/<zone>``,
``api.cloudflare.com/client/v4/accounts/<id>``), ``mend.io`` its SaaS consoles
(``saas.mend.io``, ``app.mend.io``) and ``renovatebot.com`` the hosted app's dashboard
(``app.renovatebot.com/dashboard#github/<org>/<repo>``): only their documentation and CDN
hosts and their apex and ``www`` sites (the vendor pages a bot footer links,
``https://www.mend.io/``) are public, each exactly; any other host under them is unknown.
"""

TENANT_ROUTES: dict[str, frozenset[str]] = {"stackoverflow.com": frozenset({"c"})}
"""First path segments that open a tenant surface on a public host, by registrable suffix (PR #319 round 4).

Stack Overflow for Teams serves a private team under ``stackoverflow.com/c/<team>/…``: the
team and its questions are a customer's, so a URL on that route is not public.
"""
