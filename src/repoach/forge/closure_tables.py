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
suffix is listed by its documentation and CDN hosts instead (:data:`PUBLIC_HOSTS`), a tenant
or account route on a listed host is :data:`TENANT_ROUTES`, and a listed host whose first path
segment is an account is :data:`ACCOUNT_NAMESPACES` (round 6). Audited, every suffix here: the
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

FORGE_PATH_HOSTS: tuple[str, ...] = (
    "bitbucket.org",
    "codeberg.org",
    "framagit.org",
    "git.sr.ht",
    "gitea.com",
    "gitee.com",
    "github.com",
    "gitlab.com",
    "gopkg.in",
    "launchpad.net",
)
"""Code forges whose ``<host>/<owner>/…`` path another public host embeds (PR #319 round 7, 4687852992).

A Go module path is its repository's address: ``pkg.go.dev/github.com/<owner>/<repo>``,
``proxy.golang.org/github.com/<owner>/<repo>/@v/list`` and
``sum.golang.org/lookup/github.com/<owner>/<repo>@<v>`` name the owner as ``github.com/<owner>``
does. Each forge here is a route of the Go hosts (:data:`TENANT_ROUTES`), its next segment the
identity. ``framagit.org`` is a GitLab instance (its front page: https://framagit.org/), and
``gopkg.in/<user>/<pkg>.v<N>`` is ``github.com/<user>/<pkg>`` while ``gopkg.in/<pkg>.v<N>`` names
no account (https://labix.org/gopkg.in; :data:`TENANT_ROUTE_PUBLIC_SHAPES`).
"""

GO_MODULE_HOSTS: tuple[str, ...] = (*FORGE_PATH_HOSTS, "forge.example.com")
"""The forge hosts a Go host's route names: :data:`FORGE_PATH_HOSTS` and the placeholder host.

The recorder writes a known forge host as ``forge.example.com`` wherever it stands, so
``proxy.golang.org/github.com/<owner>/…`` reaches the oracle as
``proxy.golang.org/forge.example.com/<owner>/…``: the owner is the same identity under either
spelling.
"""

GO_PATH_PREFIXES: tuple[str, ...] = (
    "badge",
    "files",
    "mod",
    "v1/imported-by",
    "v1/module",
    "v1/package",
    "v1/packages",
    "v1/symbols",
    "v1/versions",
)
"""What stands before a module path on pkg.go.dev (PR #319 round 10, 4687852992): its badge, module, file and API routes.

The vendor's own route table:
https://raw.githubusercontent.com/golang/pkgsite/master/internal/frontend/server.go (``GET /badge/``,
``GET /mod/``, ``GET /files/``, ``GET /v1/package/`` and the rest).
"""

_MOZILLA_WIKI_NAMESPACES = ("special:", "user:", "user_talk:")
"""The namespaces of the Mozilla wiki that name an account; its other ``<word>:<title>`` pages are projects'."""

TENANT_ROUTES: dict[str, frozenset[str]] = {
    "apache.org": frozenset({"confluence/*/~", "confluence/~", "roster/committer"}),
    "bugs.python.org": frozenset({"user#"}),
    "crates.io": frozenset({"api/v1/teams", "api/v1/users", "teams", "users"}),
    "docs.rs": frozenset({"releases"}),
    "go.dev": frozenset(
        {*GO_MODULE_HOSTS, *(f"{p}/{h}" for p in GO_PATH_PREFIXES for h in GO_MODULE_HOSTS)}
    ),
    "golang.org": frozenset(
        {
            *GO_MODULE_HOSTS,
            *(f"{p}/{h}" for p in ("cached-only", "lookup") for h in GO_MODULE_HOSTS),
        }
    ),
    "hg.mozilla.org": frozenset({"users"}),
    "jsdelivr.net": frozenset({"combine", "gh"}),
    "mozilla.org": frozenset(
        {"*/android/user", "*/firefox/user", "android/user", "firefox/user", "p", "u"}
    ),
    "npmjs.com": frozenset({"org", "settings"}),
    "npmjs.org": frozenset({"-/org", "-/team", "-/user"}),
    "npmmirror.com": frozenset({"-/org", "-/team", "-/user"}),
    "nuget.org": frozenset({"profiles"}),
    "pontoon.mozilla.org": frozenset({"contributor", "contributors"}),
    "pypi.org": frozenset({"manage/organization", "org", "user"}),
    "python.org": frozenset({"u"}),
    "readthedocs.org": frozenset({"organizations", "profiles"}),
    "reviews.apache.org": frozenset({"users"}),
    "stackoverflow.com": frozenset({"c", "cv", "story", "users", "users/*"}),
    "support.mozilla.org": frozenset({"*/user", "user"}),
    "wiki.mozilla.org": frozenset(
        {f"{lead}{ns}" for lead in ("", "index.php/") for ns in _MOZILLA_WIKI_NAMESPACES}
    ),
    "wikipedia.org": frozenset({"w/index.php/*:", "wiki/*:"}),
    "yarnpkg.com": frozenset({"-/org", "-/team", "-/user"}),
}
"""Routes that open a tenant surface or name an account on a public host, by registrable suffix (PR #319 rounds 4, 6 and 7).

A route is its path segments joined by ``/``; ``*`` stands for any one segment (a locale). The
segment after the route is the identity: ``stackoverflow.com/c/<team>`` (Stack Overflow for
Teams, a customer's private team), a PyPI user, organisation or organisation console, a
crates.io user or team (pages and API), a NuGet profile, an npm organisation or settings page,
an npm registry's CouchDB user, organisation and team routes
(``registry.npmjs.org/-/user/org.couchdb.user:<login>``, mirrored by the yarn and npmmirror
registries), jsDelivr's GitHub proxy (``cdn.jsdelivr.net/gh/<owner>/<repo>``, and
``combine``, whose list may hold one), a docs.rs owner (``docs.rs/releases/<owner>``), a
Discourse profile on ``discuss.python.org`` and ``discourse.mozilla.org`` (``u/<user>``), a
Mozilla people page and add-on author (``people.mozilla.org/p/<user>``,
``addons.mozilla.org/<locale>/firefox/user/<id>``), an Apache committer
(``whimsy.apache.org/roster/committer/<id>``), a Read the Docs profile or organisation, and a
forge path a Go host embeds (:data:`FORGE_PATH_HOSTS`). A route whose last segment ends in ``:``
holds its identity in that same segment: on Wikipedia ``wiki/<namespace>:<title>`` is a user
page (``wiki/User:<name>``, ``wiki/Benutzer:<name>``, ``Special:Contributions/<name>``) unless
the namespace is a public one (:data:`TENANT_ROUTE_PUBLIC`), whatever the language names it; on
the Mozilla wiki the namespaces are written out. A route whose last segment is ``~`` names the
account in a segment led by it: a personal space of Apache Confluence
(``cwiki.apache.org/confluence/display/~<id>``, ``spaces/~<id>``), never a project's space
(``display/KAFKA``). A Mozilla support profile is ``user/<name>`` after an optional locale, and
Mozilla's Mercurial host and Apache's Review Board name one after ``users``. A
Stack Overflow profile names its user in the segment after the id (``users/<id>/<name>``), or,
with none, by the id alone (``users/<id>``, PR #319 round 9, 4689483333), and a developer story
in the segment after ``story`` or ``cv``. A route whose last segment ends in ``#`` names an account
by its number in that segment (round 10): ``bugs.python.org/user<id>``, an item designator of
Roundup (https://www.roundup-tracker.org/docs/reference.html, "Roundup URL design"). A Pontoon
profile is ``contributors/<username>`` or ``contributor/<email>``
(https://raw.githubusercontent.com/mozilla/pontoon/main/pontoon/contributors/urls.py), and the
Go module mirror serves a module path after ``cached-only`` too (https://proxy.golang.org/). The same accounts named in a query are
:data:`TENANT_QUERIES`. The redactor rewrites a tenant URL through the forge route grammar and keeps it only when no
identity survives the rewrite; one the grammar cannot place (a CouchDB user, a profile name) is
left as it stands, and the oracle refuses it.
"""

_WIKIPEDIA_PUBLIC_NAMESPACES = frozenset(
    {
        "category",
        "draft",
        "file",
        "help",
        "image",
        "mediawiki",
        "module",
        "portal",
        "talk",
        "template",
        "timedtext",
        "wikipedia",
        "wp",
    }
)

TENANT_ROUTE_PUBLIC: dict[str, frozenset[str]] = {
    "go.dev forge.example.com": frozenset({"org"}),
    **{f"go.dev {p}/forge.example.com": frozenset({"org"}) for p in GO_PATH_PREFIXES},
    "go.dev ?forge.example.com/": frozenset({"org"}),
    "golang.org forge.example.com": frozenset({"org"}),
    "golang.org cached-only/forge.example.com": frozenset({"org"}),
    "golang.org lookup/forge.example.com": frozenset({"org"}),
    "docs.rs releases": frozenset(
        {"activity", "failures", "feed", "queue", "recent", "recent-failures", "search", "stars"}
    ),
    "stackoverflow.com users": frozenset({"account-recovery", "login", "signup"}),
    "wikipedia.org w/index.php/*:": _WIKIPEDIA_PUBLIC_NAMESPACES,
    "wikipedia.org wiki/*:": _WIKIPEDIA_PUBLIC_NAMESPACES,
    "wikipedia.org ?*:": _WIKIPEDIA_PUBLIC_NAMESPACES,
}
"""The identities a route names that are public pages, by ``"<suffix> <route>"`` (PR #319 round 7).

``org`` after the placeholder host on a Go host is the recorder's own placeholder namespace, an
identity already redacted. ``docs.rs/releases/recent`` and its siblings are docs.rs's own
listings, not an owner, and ``stackoverflow.com/users/login`` and its siblings are its forms.
On Wikipedia a namespace of articles, categories, help, templates, files, portals, modules,
drafts and the project's own pages, or its talk namespace (``<namespace>_talk``), is public;
every other namespace (``User``, ``User_talk``, ``Special``, a localised user namespace) is
refused, in a path and, under the key ``?*:``, in a query.
"""

TENANT_ROUTE_PUBLIC_SHAPES: dict[str, str] = {"gopkg.in": r"[^/\s?#\"'<>]*\.v[0-9]"}
"""The shape of a segment that names no account after a route, by the route's last segment (PR #319 round 10).

``gopkg.in/yaml.v3`` is the package ``go-yaml/yaml``'s own address, ``gopkg.in/<user>/pkg.v3`` a
user's (https://labix.org/gopkg.in).
"""

_PAGE_PARAMETERS = ("page", "title", "titles")
"""The query parameters of MediaWiki that name a page (``index.php?title=``, ``api.php?titles=``, ``page=``)."""

_WIKI_API_USERS = ("ucuser", "ucuserids", "ucuserprefix", "ususerids", "ususers")
"""The parameters of MediaWiki's ``usercontribs`` and ``users`` modules that name an account."""

_URL_LEADS = ("", "http://", "https://")

TENANT_QUERIES: dict[str, frozenset[tuple[str, str, str]]] = {
    "apache.org": frozenset(
        {
            ("", "key", "~"),
            ("", "spacekey", "~"),
            ("", "username", ""),
            ("phonebook.html", "uid", ""),
            ("phonebook.html", "user", ""),
            ("viewprofile.jspa", "name", ""),
        }
    ),
    "bugzilla.mozilla.org": frozenset(
        {("user_profile", "login", ""), ("user_profile", "user_id", "")}
    ),
    "go.dev": frozenset(
        {
            *(("search", "q", f"{h}/") for h in GO_MODULE_HOSTS),
            *(("badge/", "path", f"{lead}{h}/") for lead in _URL_LEADS for h in GO_MODULE_HOSTS),
        }
    ),
    "npmjs.org": frozenset(
        {("-/v1/search", "text", "+author:"), ("-/v1/search", "text", "+maintainer:")}
    ),
    "nuget.org": frozenset({("", "q", "+author:"), ("", "q", "+owner:")}),
    "prow.k8s.io": frozenset({("", "author", ""), ("pr", "query", "+author:")}),
    "wiki.mozilla.org": frozenset(
        {("", name, ns) for name in _PAGE_PARAMETERS for ns in _MOZILLA_WIKI_NAMESPACES}
    ),
    "wikipedia.org": frozenset(
        {
            *(("", name, "*:") for name in _PAGE_PARAMETERS),
            *(("api.php", name, "") for name in _WIKI_API_USERS),
        }
    ),
}
"""Query parameters that name an account on a public host, by suffix (PR #319 round 9, 4689483333).

Each is ``(page, parameter, value)``, in any case: ``page`` is what the path must end with, ``""``
for any. The value is an account whatever it is (``""``: Confluence's
``viewuserprofile.action?username=<id>``, Jira's ``ViewProfile.jspa?name=<id>``, Bugzilla's
``user_profile?login=<login>``), one led by ``~``
(a personal space, ``viewspace.action?key=~<id>``), or ``<namespace>:<title>`` read as the path
form of :data:`TENANT_ROUTES` is (``w/index.php?title=User:<name>``). One led by ``+`` is a
qualifier that may follow other words of a search (``q=json+owner:<account>``). Round 10
(4687852992), each with the vendor page it is read from:

- Apache's phonebook, ``?uid=`` and ``?user=``: https://home.apache.org/phonebook-about.html
- MediaWiki's API: https://www.mediawiki.org/wiki/API:Usercontribs and
  https://www.mediawiki.org/wiki/API:Users
- an npm registry's ``text=author:`` and ``maintainer:``:
  https://raw.githubusercontent.com/npm/registry/main/docs/REGISTRY-API.md
- NuGet's ``q=owner:`` and ``author:``:
  https://learn.microsoft.com/en-us/nuget/consume-packages/finding-and-choosing-packages
- Prow's ``?author=`` and ``pr?query=…author:``:
  https://raw.githubusercontent.com/kubernetes-sigs/prow/main/cmd/deck/static/prow/prow.ts and
  https://raw.githubusercontent.com/kubernetes-sigs/prow/main/cmd/deck/static/pr/pr.ts
- a module path searched on pkg.go.dev, which redirects to its page, or handed to its badge tool:
  https://raw.githubusercontent.com/golang/pkgsite/master/internal/frontend/search.go and
  https://raw.githubusercontent.com/golang/pkgsite/master/internal/frontend/badge.go
"""


ACCOUNT_NAMESPACES: dict[str, tuple[str, frozenset[str]]] = {
    "apache.org": ("~", frozenset()),
    "docker.io": ("", frozenset({"library"})),
    "ghcr.io": ("", frozenset()),
    "npmjs.com": ("~", frozenset()),
}
"""Public hosts whose first path segment is an account, by registrable suffix: its lead and the namespaces no account holds (PR #319 rounds 6 and 7).

The namespace segment is an identity slot, as ``github.com/<owner>`` is: ``ghcr.io/<owner>/…``
is a GitHub login or organisation, ``docker.io/<namespace>/…`` (``registry-1.docker.io``
included) a Docker Hub user or organisation, ``npmjs.com/~<user>`` an npm user and
``people.apache.org/~<id>`` (``home.apache.org`` too) an Apache committer. An OCI
distribution API path names it after ``v2/`` (``ghcr.io/v2/<owner>/<image>/manifests/…``).
``library`` on ``docker.io`` holds the Docker Official Images and is no account. ``k8s.io``
is not listed: every path under it is a Kubernetes project, which no account can publish to.
"""
