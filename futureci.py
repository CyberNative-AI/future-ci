#!/usr/bin/env python3
"""futureci.py: run a repository's own tests under dated upcoming conditions and report
which upcoming change breaks it, and when.

  plan    read the repo's workflows and version files: static findings per condition and the
          list of runs (condition run + same-day baseline run) to execute
  exec    run one phase (install/test) of one run, record exit code, log tail, failing tests
  record  record the outcome of a step that ran outside exec (e.g. a setup-* action)
  tz      install the condition's tz database into the system zoneinfo (needs root or sudo), build the
          pinned libfaketime and check that a test process sees the condition's clock
  report  merge plan + run results into a "what breaks and when" report (Markdown + JSON)
  local   execute a plan in local Docker containers with the real setup-* actions (no Actions minutes)

Conditions live in conditions.json next to this file. Only `plan` needs PyYAML.
`plan` re-reads PyPI for the dependencies the Python 3.15 snapshot flags; FUTURECI_PYPI=off keeps it offline.
"""
import argparse, ast, datetime as dt, json, os, re, shutil, signal, subprocess, sys, tempfile, threading
import time, urllib.request, xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
REMOVED_315 = {"sre_compile", "sre_constants", "sre_parse"}
# The ubuntu-24.04 libfaketime (0.9.10) fails Python's time.sleep and subprocess timeouts with EINVAL
# while the monotonic clock stays real, and faking monotonic instead hangs every lock, queue and join
# timeout. The tz condition therefore preloads this pinned build, and only after faketime_probe passes.
# (0.9.12 does not compile with the gcc 15 on ubuntu-26.04.) Upstream's default semaphore is an flock on an
# inherited fd: a process that closes its fds (daemons, test helpers, os.closerange) then exits 1 from its next
# stat(). The POSIX semaphore lives in mapped memory and keeps the clock shared across child processes.
LIBFAKETIME = {"version": "0.9.13", "url": "https://github.com/wolfcw/libfaketime/archive/refs/tags/v0.9.13.tar.gz",
               "sha256": "8e56deeb805682b025107e095f1d94a6ea677472f05824cd475f5c5e6e1a5ddf",
               "build": "posixsem", "cflags": "-Wno-error -DFT_SEMAPHORE_BACKEND=FT_POSIX"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", ".tox", ".nox", "site-packages", "build", "dist",
             "vendor", "third_party", "__pycache__"}
TZ_ZONES = re.compile(r"\b(America/(Edmonton|Calgary|Inuvik|Yellowknife|Winnipeg|Vancouver|Whitehorse|Dawson_Creek|"
                      r"Fort_Nelson|Rankin_Inlet|Resolute)|Canada/(Mountain|Central|Pacific))\b")


def load_conditions(path=None):
    with open(path or os.path.join(HERE, "conditions.json"), encoding="utf-8") as f:
        return {c["id"]: c for c in json.load(f)["conditions"]}


# ------------------------------------------------------------------ PEP 440
_PEP440 = re.compile(r"""v?(?:(?P<epoch>[0-9]+)!)?(?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?:-(?P<post_n1>[0-9]+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?)?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?""", re.VERBOSE | re.IGNORECASE | re.ASCII)
_LOCAL = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", re.IGNORECASE | re.ASCII)
_PREFIX = re.compile(r"v?(?:(?P<epoch>[0-9]+)!)?(?P<release>[0-9]+(?:\.[0-9]+)*)\.\*", re.IGNORECASE | re.ASCII)
_CLAUSE = re.compile(r"(===|~=|==|!=|<=|>=|<|>)\s*(.*)", re.DOTALL)


def _key(m):
    rel = [int(x) for x in m["release"].split(".")]
    while len(rel) > 1 and rel[-1] == 0:
        rel.pop()
    pre_l = (m["pre_l"] or "").lower()
    pre = None if not pre_l else ({"alpha": 0, "a": 0, "beta": 1, "b": 1}.get(pre_l, 2), int(m["pre_n"] or 0))
    post = m["post_n1"] or (m["post_n2"] or "0" if m["post_l"] else None)
    dev = None if not m["dev_l"] else int(m["dev_n"] or 0)
    key = (int(m["epoch"] or 0), tuple(rel),
           (-1,) if pre is None and post is None and dev is not None else (2,) if pre is None else (0,) + pre,
           (-1,) if post is None else (0, int(post)),
           (1,) if dev is None else (0, dev))
    return pre is not None or dev is not None, key


def pep440_key(v):
    """(is_prerelease, sort key) in PEP 440 order, or None if `v` is not a public PEP 440 version (pip skips it)."""
    m = _PEP440.fullmatch(v.strip())
    return _key(m) if m else None


def spec_allows(spec, version):
    """True/False if the requires-python `spec` admits the final release `version` (X.Y.Z) by the PEP 440
    rules pip applies; None if `spec` is not a valid specifier set (pip ignores it; callers treat it as undecided)."""
    v = pep440_key(version)[1]
    rel = [0] + [int(x) for x in version.split(".")]  # epoch 0, release unstripped (prefix matching pads it)

    def prefix(epoch, release):
        want = [int(epoch or 0)] + [int(x) for x in release.split(".")]
        return (rel + [0] * len(want))[:len(want)] == want

    ok = True
    for clause in [c.strip() for c in spec.split(",") if c.strip()]:
        m = _CLAUSE.fullmatch(clause)
        if not m:
            return None
        op, ver = m.groups()
        if op == "===":
            if re.search(r"[\s;)]", ver):
                return None
            ok = ok and ver.lower() == version
            continue
        if op in ("==", "!=") and ver.endswith(".*"):
            p = _PREFIX.fullmatch(ver)
            if not p:
                return None
            ok = ok and prefix(p["epoch"], p["release"]) == (op == "==")
            continue
        pub, plus, local = ver.partition("+")
        pm = _PEP440.fullmatch(pub)
        if not pm or (plus and (op not in ("==", "!=") or not _LOCAL.fullmatch(local))):
            return None
        w = _key(pm)[1]
        if op == "~=":
            if not re.fullmatch(r"(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)+", ver):
                return None  # pip derives ~='s prefix from the spelling (~=V3.4, ~=3.1c0): not decided here
            hit = v >= w and prefix(pm["epoch"], pm["release"].rsplit(".", 1)[0])
        elif op in ("==", "!="):
            hit = (v == w and not plus) == (op == "==")  # `version` has no local label
        else:  # `version` is final: <V's pre-release and >V's post-release exclusions cannot apply
            hit = {"<=": v <= w, ">=": v >= w, "<": v < w, ">": v > w}[op]
        ok = ok and hit
    return ok


# ------------------------------------------------------------------ static scans
def walk_files(root, exts, limit=20000):
    n = 0
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in files:
            if f.endswith(exts):
                p = os.path.join(d, f)
                if os.path.islink(p):
                    continue
                n += 1
                if n > limit:
                    return
                yield p


def read_small(p, cap=1 << 20):
    try:
        if os.path.getsize(p) > cap:
            return None
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def requires_python(root):
    """(spec, file) from pyproject.toml / setup.cfg / setup.py, or (None, None)."""
    for rel, rx in (("pyproject.toml", r'(?m)^\s*requires-python\s*=\s*["\']([^"\']+)["\']'),
                    ("setup.cfg", r"(?m)^\s*python_requires\s*=\s*(.+)$"),
                    ("setup.py", r'python_requires\s*=\s*["\']([^"\']+)["\']')):
        t = read_small(os.path.join(root, rel))
        if t:
            m = re.search(rx, t)
            if m:
                return m.group(1).strip(), rel
    return None, None


def removed_imports(root):
    hits = []
    for p in walk_files(root, (".py",)):
        t = read_small(p)
        if not t or "sre_" not in t:
            continue
        try:
            tree = ast.parse(t)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module.split(".")[0]]
            for nm in names:
                if nm in REMOVED_315:
                    guarded = _in_try(tree, node)
                    hits.append((os.path.relpath(p, root), node.lineno, nm, guarded))
    return hits


def _in_try(tree, target):
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(target is n for b in node.body for n in ast.walk(b)):
            return any(isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "ModuleNotFoundError")
                       or h.type is None for h in node.handlers)
    return False


def declared_deps(root):
    """Lower-cased distribution names from requirements*.txt and pyproject dependency lists."""
    names = {}
    files = [p for p in walk_files(root, (".txt", ".in")) if re.search(r"requirements", os.path.basename(p))]
    files += [os.path.join(root, "pyproject.toml")]
    for p in files:
        t = read_small(p)
        if not t:
            continue
        rel = os.path.relpath(p, root)
        if p.endswith(".toml"):
            body = "\n".join(re.findall(r'(?s)(?:dependencies|requires)\s*=\s*\[(.*?)\]', t))
            cands = re.findall(r'["\']\s*([A-Za-z0-9][A-Za-z0-9._-]*)', body)
        else:
            cands = [re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", ln).group(1) for ln in t.splitlines()
                     if re.match(r"\s*[A-Za-z0-9]", ln) and not ln.lstrip().startswith(("-", "#"))]
        for c in cands:
            names.setdefault(re.sub(r"[-_.]+", "-", c.lower()), rel)
    return names


# data/top3000-*.tsv: Python 3.15 installability of PyPI's most downloaded projects, as of this date.
# Projects keep shipping cp315 wheels and lifting caps, so plan re-reads PyPI for the ones a repo declares
# and the snapshot flags. Only those public names are sent to PyPI, never the repo's other dependencies.
PY315_SNAPSHOT = "2026-10-11"
PY315_RISK_CLASSES = ("older_only", "sdist_only", "no_linux_x86_wheel")
PYPI_JSON = "https://pypi.org/pypi/{}/json"
PYPI_MAX, PYPI_TIMEOUT, PYPI_BYTES = 40, 10, 64 << 20  # the largest flagged answer (ddtrace) is 39 MB


def pypi_json(name):
    req = urllib.request.Request(PYPI_JSON.format(name), headers={"Accept": "application/json", "User-Agent": "future-ci"})
    with urllib.request.urlopen(req, timeout=PYPI_TIMEOUT) as r:
        body = r.read(PYPI_BYTES + 1)
    if len(body) > PYPI_BYTES:
        raise ValueError("PyPI answer too large")
    return json.loads(body)


PY315_GLIBC = (2, 39)  # the py315 job runs on ubuntu-24.04
SDIST_EXTS = (".tar.gz", ".tgz", ".tar", ".zip", ".tar.bz2", ".tbz", ".tar.xz", ".txz", ".tlz", ".tar.lz", ".tar.lzma")


def py3_tag_ok(py):
    """pip accepts an abi-none wheel on CPython 3.15 if its Python tag is py3 or py3X with X <= 15."""
    m = re.fullmatch(r"py3(\d*)", py)
    return bool(m) and (not m[1] or int(m[1]) <= 15)


def plat_ok(plat):
    """True if pip on CPython 3.15, glibc PY315_GLIBC, x86_64 accepts this platform tag (musllinux never)."""
    if plat in ("any", "linux_x86_64", "manylinux1_x86_64", "manylinux2010_x86_64", "manylinux2014_x86_64"):
        return True
    m = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", plat)
    return bool(m) and int(m[1]) == PY315_GLIBC[0] and 5 <= int(m[2]) <= PY315_GLIBC[1]


def tag_ok(py, abi, plat):
    """One expanded wheel tag against pip's CPython 3.15 tag list (packaging.tags: cpython_tags, compatible_tags)."""
    if not plat_ok(plat):
        return False
    if abi == "none":
        return py3_tag_ok(py) or py == "cp315"
    if plat == "any":
        return False
    if abi == "cp315":
        return py == "cp315"
    m = re.fullmatch(r"cp3(\d+)", py)
    return abi == "abi3" and bool(m) and 2 <= int(m[1]) <= 15


def admits_315(f):
    """True/False if this file's own requires-python admits 3.15.0 (none admits); None if it cannot be decided."""
    rp = f.get("requires_python")
    return spec_allows(str(rp), "3.15.0") if rp else True


def file_live(f):
    """True for an unyanked file that admits 3.15.0, False if yanked or excluded, None if undecided."""
    return False if f.get("yanked") else admits_315(f)


def wheel_tags(filename):
    m = re.fullmatch(r".+-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl", filename)
    if not m:
        return []
    return [(py, abi, plat) for py in m["py"].split(".") for abi in m["abi"].split(".") for plat in m["plat"].split(".")]


def wheel_class(files):
    """What CPython 3.15 on Linux x86_64 (glibc) can install from a release's files without a build
    (the classes of data/top3000-wheels.tsv). Only unyanked files that admit 3.15 and carry a tag pip
    accepts there count; anything else leaves the release at a risk class, or "undecided" if the answer
    rests on a requires-python that cannot be decided."""
    if not files:
        return "no_files"
    live = [str(f.get("filename", "")) for f in files if file_live(f)]
    whl = [w for w in live if w.endswith(".whl")]
    ok = [t for w in whl for t in wheel_tags(w) if tag_ok(*t)]
    if any(plat == "any" for _, _, plat in ok):
        return "pure"
    if any(py == "cp315" and abi in ("cp315", "none") for py, abi, _ in ok):
        return "cp315"
    if any(abi == "abi3" for _, abi, _ in ok):
        return "abi3"
    if ok:
        return "pure"
    if any(tag_ok(*t) for f in files if file_live(f) is None for t in wheel_tags(str(f.get("filename", "")))):
        return "undecided"  # an installable wheel whose requires-python cannot be read: neither dropped nor asserted
    if any(re.fullmatch(r"(many)?linux(\w*)_x86_64", plat) for w in whl for _, _, plat in wheel_tags(w)):
        return "older_only"
    if whl:
        return "no_linux_x86_wheel"
    return "sdist_only" if any(w.endswith(SDIST_EXTS) for w in live) else "no_files"


def file_usable(f):
    """pip on CPython 3.15 / Linux x86_64 can use this file (yanked or not): a compatible wheel or an sdist
    that admits 3.15.0. None if it is one of those but its requires-python cannot be decided."""
    name = str(f.get("filename", ""))
    if not (any(tag_ok(*t) for t in wheel_tags(name)) if name.endswith(".whl") else name.endswith(SDIST_EXTS)):
        return False
    return admits_315(f)


UNDECIDED = "undecided"


def fallback_315(releases):
    """The release an unpinned `pip install` takes on CPython 3.15.0 / Linux x86_64. pip's finder keeps PEP 440
    versions with a usable file (per file, yanked files included) and only final ones unless no final one
    qualifies; its resolver then skips yanked files (PEP 592) and takes the highest version left.
    None if nothing is left; UNDECIDED if a file whose requires-python cannot be decided could change the answer."""
    sure, unsure = {}, {}  # version key -> [is_prerelease, version, has an unyanked usable file]
    for v, files in releases.items():
        k = pep440_key(str(v))
        for f in (files or []) if k else []:
            u = file_usable(f)
            if u is not False:
                r = (sure if u else unsure).setdefault(k[1], [k[0], str(v), False])
                r[2] = r[2] or not f.get("yanked")
    finals = any(not pre for pre, _, _ in sure.values())
    best = max(((key, v) for key, (pre, v, live) in sure.items() if live and not (pre and finals)), default=None)
    for key, (pre, v, live) in unsure.items():
        if (not pre and not finals) or (live and not (pre and finals) and (best is None or key > best[0])):
            return UNDECIDED
    return best[1] if best else None


def py315_now(name, fetch=pypi_json):
    """PyPI's current latest release of `name` as CPython 3.15 sees it, or None if PyPI cannot be read.
    "undecided" is set when the answer rests on a requires-python that cannot be decided."""
    try:
        d = fetch(name)
        info = d["info"]
        version, rp = str(info["version"]), str(info.get("requires_python") or "")
        state = {"version": version, "requires_python": rp, "class": wheel_class(d.get("urls") or [])}
        state["allows"] = spec_allows(rp, "3.15.0") if rp else True
        source = {file_live(f) for f in d.get("urls") or [] if str(f.get("filename", "")).endswith(SDIST_EXTS)}
        if state["allows"] is None or state["class"] == "undecided":
            state["undecided"] = True
        elif not state["allows"]:
            state["fallback"] = fallback_315(d.get("releases") or {})
        elif state["class"] in PY315_RISK_CLASSES + ("no_files",) and True not in source:
            if None in source:
                state["undecided"] = True
            else:
                state["no_source"] = True
                state["fallback"] = fallback_315(d.get("releases") or {})
        return state
    except Exception:
        return None


def recheck_pypi(names, fetch=pypi_json):
    """{name: py315_now state or None}; None for every name when FUTURECI_PYPI=off and past PYPI_MAX."""
    names = list(names)
    if os.environ.get("FUTURECI_PYPI", "").lower() in ("0", "off", "no", "false") or not names:
        return {n: None for n in names}
    from concurrent.futures import ThreadPoolExecutor
    head = names[:PYPI_MAX]
    with ThreadPoolExecutor(max_workers=min(8, len(head))) as ex:
        got = dict(zip(head, ex.map(lambda n: py315_now(n, fetch), head)))
    return {n: got.get(n) for n in names}


def falls_back(fallback):
    if fallback == UNDECIDED:
        return "pip's fallback on 3.15 is not decidable (a requires-python on PyPI cannot be read)"
    return f"pip falls back to {fallback or 'nothing'} on 3.15"


def py315_dep_findings(deps, capped, wheels, fetch=pypi_json):
    """RISK findings for declared dependencies the snapshot flags, re-read from PyPI. A dependency that
    has since lifted its cap or shipped an installable wheel is dropped; if PyPI cannot be read, or its
    answer rests on a requires-python that cannot be decided, the snapshot finding stays and says so."""
    flagged = [(n, rel) for n, rel in sorted(deps.items())
               if n in capped or (n in wheels and wheels[n]["class"] in PY315_RISK_CLASSES)]
    live = recheck_pypi([n for n, _ in flagged], fetch)
    out = []
    for name, rel in flagged:
        s = live.get(name)
        if s is None or s.get("undecided"):
            if name in capped:
                r = capped[name]
                cause = (f"{name} {r['latest']} requires-python {r['latest_requires_python']}: "
                         f"pip falls back to {r['fallback_on_3.15'] or 'nothing'} on 3.15")
            else:
                cause = f"{name} {wheels[name]['version']} has no cp315 wheel: builds from source on 3.15"
            cause += (f" (PyPI not read; snapshot of {PY315_SNAPSHOT})" if s is None else
                      f" (PyPI's requires-python not decidable; snapshot of {PY315_SNAPSHOT})")
        elif not s["allows"]:
            cause = f"{name} {s['version']} requires-python {s['requires_python']}: {falls_back(s['fallback'])}"
        elif s.get("no_source"):
            cause = f"{name} {s['version']} has no cp315 wheel and no source release: {falls_back(s['fallback'])}"
        elif s["class"] in PY315_RISK_CLASSES or s["class"] == "no_files":
            cause = f"{name} {s['version']} has no cp315 wheel: builds from source on 3.15"
        else:
            continue
        out.append({"condition": "python-3.15", "status": "RISK", "where": rel, "cause": cause})
    return out


def load_tsv(name):
    p = os.path.join(HERE, "data", name)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        hdr = f.readline().rstrip("\n").split("\t")
        return {r["project"]: r for r in (dict(zip(hdr, ln.rstrip("\n").split("\t"))) for ln in f)}


def workflow_docs(root):
    import yaml
    wf = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(wf):
        return []
    out = []
    for n in sorted(os.listdir(wf)):
        p = os.path.join(wf, n)
        if n.endswith((".yml", ".yaml")) and not os.path.islink(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    out.append((f".github/workflows/{n}", yaml.safe_load(f)))
            except Exception:
                out.append((f".github/workflows/{n}", None))
    return out


def scan_macos14(c26, docs):
    hits = []
    for rel, doc in docs:
        if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
            continue
        for jid, job in doc["jobs"].items():
            if not isinstance(job, dict):
                continue
            try:
                combos = c26.expand_matrix(job.get("strategy"))
            except Exception:
                combos = [{}]
            for combo in combos:
                ro = job.get("runs-on")
                try:
                    ro = c26.substitute(ro, {"matrix": combo, "env": {}}) if isinstance(ro, str) else ro
                except Exception:
                    pass
                labels = ro if isinstance(ro, list) else [ro]
                if any(isinstance(x, str) and re.fullmatch(r"macos-14(-large|-xlarge)?", x.strip()) for x in labels):
                    hits.append(f"{rel}:{jid}")
                    break
    return sorted(set(hits))


def node_specs(c26, root, docs):
    specs = set()
    for rel, doc in docs:
        if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
            continue
        for job in doc["jobs"].values():
            if not isinstance(job, dict):
                continue
            try:
                combos = c26.expand_matrix(job.get("strategy"))
            except Exception:
                continue
            for s in job.get("steps") or []:
                if not isinstance(s, dict) or s.get("uses", "").split("@")[0].lower() != "actions/setup-node":
                    continue
                w = s.get("with") or {}
                for combo in combos:
                    try:
                        if w.get("node-version") not in (None, ""):
                            specs.add(str(c26.substitute(w["node-version"], {"matrix": combo, "env": {}})))
                        elif w.get("node-version-file"):
                            t = read_small(os.path.join(root, w["node-version-file"]))
                            if t and t.strip():
                                specs.add(t.strip().splitlines()[0].lstrip("v"))
                    except Exception:
                        pass
    return sorted(specs)


def node_major(spec):
    m = re.match(r"v?(\d+)", spec)
    return int(m.group(1)) if m else None


def select_conditions(conds, only=None):
    """Condition ids to check: the default set (every condition not marked optIn) unless ids are named;
    the name `default` stands for the default set, so `default canada-tz-2026-11-01` adds one."""
    default = [cid for cid, c in conds.items() if not c.get("optIn")]
    if not only:
        return default
    unknown = [x for x in only if x != "default" and x not in conds]
    if unknown:
        raise SystemExit(f"futureci: unknown condition(s): {' '.join(unknown)} (known: default {' '.join(conds)})")
    sel = []
    for x in only:
        sel += [cid for cid in (default if x == "default" else [x]) if cid not in sel]
    return sel


def plan(root, conds, data_dir, only=None, test_lang=None):
    sys.path.insert(0, os.path.join(HERE, "vendor"))
    import check2604 as c26
    data = c26.Data(data_dir, False)
    docs = workflow_docs(root)
    static, runs, langs = [], [], {}
    selected = select_conditions(conds, only)
    want = lambda cid: cid in selected

    # ubuntu-26.04 (static: the published setup checker) + versions used by the repo
    findings = c26.check_repo(data, root)
    py, rb = {}, {}
    for st, act, spec, where, soft in findings:
        if want("ubuntu-26.04") and st in ("BREAK", "UNKNOWN"):
            static.append({"condition": "ubuntu-26.04", "status": st, "where": where,
                           "cause": f"{act} {spec}: no build for Ubuntu 26.04" if st == "BREAK" else "input not resolvable statically",
                           "soft": bool(soft)})
        if st in ("BREAK", "OK") and spec:
            try:
                if act == "setup-python" and not spec.startswith(("pypy", "graalpy")):
                    v = c26.resolve_python(data, spec, "24.04")
                    if v:
                        py[spec] = (c26.parse_ver(v), st, v)
                elif act == "setup-ruby":
                    e, v, s = c26.resolve_ruby(data, spec, "24.04")
                    if e == "ruby" and v and s == 200:
                        rb[spec] = (c26.parse_ver(v), st, v)
            except Exception:
                pass
    nodes = node_specs(c26, root, docs)
    for lang, specs in (("python", py), ("ruby", rb)):
        if specs:
            order = sorted(specs.items(), key=lambda kv: kv[1][0])
            langs[lang] = {"specs": sorted(specs), "oldest": order[0][0], "newest": order[-1][0],
                           "oldestStatus": order[0][1][1], "newestStatus": order[-1][1][1],
                           "resolved": {k: v[2] for k, v in order}}
    majors = sorted({m for m in map(node_major, nodes) if m})
    if majors or any(s.startswith("lts") for s in nodes):
        langs["node"] = {"specs": nodes, "newest": str(majors[-1]) if majors else "lts/*"}
    # the language the test command runs in: explicit, else the root manifest
    lang = test_lang or next((l for l, fs in (("python", ("pyproject.toml", "setup.py", "setup.cfg")),
                                           ("ruby", ("Gemfile",)), ("node", ("package.json",)))
                         if any(os.path.exists(os.path.join(root, f)) for f in fs)), None)
    if lang and lang not in langs:
        default = {"python": "3.14", "ruby": "3.4", "node": "24"}[lang]
        langs[lang] = {"specs": [], "oldest": default, "newest": default, "resolved": {}, "assumed": True}
    tested = {lang} if lang else set(langs)

    # python-3.15 static
    if want("python-3.15") and ("python" in langs or os.path.exists(os.path.join(root, "pyproject.toml"))
                                 or os.path.exists(os.path.join(root, "setup.py"))):
        spec, f = requires_python(root)
        if spec and spec_allows(spec, "3.14.0") and spec_allows(spec, "3.15.0") is False:
            static.append({"condition": "python-3.15", "status": "BREAK", "where": f,
                           "cause": f"requires-python {spec} excludes 3.15: pip refuses to install this project on 3.15"})
        for rel, line, mod, guarded in removed_imports(root):
            static.append({"condition": "python-3.15", "status": "RISK" if guarded else "BREAK",
                           "where": f"{rel}:{line}", "cause": f"imports {mod}, removed in Python 3.15"
                           + (" (inside try/except ImportError)" if guarded else "")})
        static += py315_dep_findings(declared_deps(root), load_tsv("top3000-capped.tsv"), load_tsv("top3000-wheels.tsv"))

    if want("macos-14-retired"):
        for w in scan_macos14(c26, docs):
            static.append({"condition": "macos-14-retired", "status": "BREAK", "where": w,
                           "cause": "job runs on macos-14, which stops running on the retirement date"})

    if want("canada-tz-2026-11-01"):
        refs = []
        for p in walk_files(root, (".py", ".rb", ".js", ".ts", ".java", ".go", ".php", ".yml", ".yaml", ".json")):
            t = read_small(p)
            if t and TZ_ZONES.search(t):
                refs.append(os.path.relpath(p, root))
        if refs:
            static.append({"condition": "canada-tz-2026-11-01", "status": "INFO", "where": ", ".join(refs[:5])
                           + (f" (+{len(refs) - 5} more)" if len(refs) > 5 else ""),
                           "cause": "code or tests name an affected Canadian zone"})

    # dynamic runs: condition run paired with a same-day baseline run
    def add(rid, cond, image, lang, version, base=None):
        runs.append({"id": rid, "condition": cond, "runsOn": f"ubuntu-{image}", "lang": lang,
                     "version": version, "baseline": base})
    for lang in ("python", "ruby"):
        if lang not in langs or lang not in tested:
            continue
        L = langs[lang]
        picks = [L["oldest"]] + ([L["newest"]] if L["newest"] != L["oldest"] else [])
        for i, spec in enumerate(picks):
            tag = "oldest" if i == 0 else "newest"
            add(f"base-{lang}-{tag}", None, "24.04", lang, spec)
            if want("ubuntu-26.04"):
                add(f"u2604-{lang}-{tag}", "ubuntu-26.04", "26.04", lang, spec, f"base-{lang}-{tag}")
        newest_base = f"base-{lang}-{'newest' if len(picks) > 1 else 'oldest'}"
        newest_v = L["resolved"].get(L["newest"], "")
        if lang == "python" and want("python-3.15"):
            if newest_v and c26.cmp_ver(c26.parse_ver(newest_v), c26.parse_ver("3.15.0")) >= 0:
                static.append({"condition": "python-3.15", "status": "TESTED", "where": L["newest"],
                               "cause": f"your matrix already runs {newest_v}"})
            else:
                add("py315", "python-3.15", "24.04", "python", conds["python-3.15"]["python"], newest_base)
        if want("canada-tz-2026-11-01"):
            add(f"tzbase-{lang}", "canada-tz-2026-11-01:zone-only", "24.04", lang, L["newest"])
            add(f"tz-{lang}", "canada-tz-2026-11-01", "24.04", lang, L["newest"], f"tzbase-{lang}")
    if "node" in langs and "node" in tested:
        add("base-node", None, "24.04", "node", langs["node"]["newest"])
        if want("node-26-lts") and langs["node"]["newest"] != conds["node-26-lts"]["node"]:
            add("node26", "node-26-lts", "24.04", "node", conds["node-26-lts"]["node"], "base-node")
        if want("canada-tz-2026-11-01") and not any(r["condition"] == "canada-tz-2026-11-01" for r in runs):
            add("tzbase-node", "canada-tz-2026-11-01:zone-only", "24.04", "node", langs["node"]["newest"])
            add("tz-node", "canada-tz-2026-11-01", "24.04", "node", langs["node"]["newest"], "tzbase-node")
    # a baseline no condition run compares against is a job that tells nothing
    used = {r["baseline"] for r in runs if r["baseline"]}
    runs = [r for r in runs if r["condition"] or r["id"] in used]
    return {"repo": os.path.abspath(root), "plannedAt": now_iso(), "conditions": selected, "languages": langs,
            "static": static, "runs": runs}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ exec / record / tz
ERR = re.compile(r"(?i)(error|exception|traceback|failed|failure|not found|cannot|could not|unable to|denied|"
                 r"no such|undefined|segmentation|killed|timed out)")
# A summary or progress line whose every error word is a zero count ("0 failures", "0 write error") is not a cause.
ZERO_BEFORE = re.compile(r"\b0 (\w+ )?$")
# Named failing tests, for runners that write no JUnit: unittest, pytest -rf, minitest, rspec, go, cargo,
# TAP/node:test and jest.
FAILED_TEST = re.compile(r"^(?:(?:FAIL|ERROR)(?: \[[\d.]+s\])?: \w+ \(\S+\)"
                         r"|FAILED \S+::\S+(?: - .*)?"
                         r"|[A-Z]\w*(?:::\w+)*#\S+ \[[^\]]+:\d+\]:?"
                         r"|rspec \./\S+:\d+ # .+"
                         r"|\s*--- FAIL: \S+(?: \([\d.]+s\))?"
                         r"|test \S+ \.\.\. FAILED"
                         r"|not ok \d+ .+"
                         r"|\s*● .+ › .+)$")


def failed_tests(lines, limit=12):
    out = []
    for ln in lines:
        if FAILED_TEST.match(ln) and ln.strip() not in out:
            out.append(ln.strip()[:300])
    return out[:limit]


def error_lines(lines, limit=12):
    def cause(ln):
        hits = list(ERR.finditer(ln))
        return hits and not all(ZERO_BEFORE.search(ln[:m.start()]) for m in hits)
    return [ln[:300] for ln in lines if cause(ln)][-limit:]


def result_path(out, rid):
    return os.path.join(out, f"{rid}.json")


def load_result(out, rid):
    p = result_path(out, rid)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"id": rid, "phases": []}


def save_result(out, res):
    os.makedirs(out, exist_ok=True)
    tmp = result_path(out, res["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    os.replace(tmp, result_path(out, res["id"]))


def record(out, rid, phase, outcome, detail="", meta=None):
    res = load_result(out, rid)
    if meta:
        res.update(meta)
    res["phases"].append({"phase": phase, "rc": 0 if outcome == "success" else 1, "outcome": outcome,
                          "detail": detail[-4000:]})
    save_result(out, res)


def condition_env(cond, conds):
    cid, _, mode = (cond or "").partition(":")
    c = conds.get(cid, {})
    if c.get("kind") != "tz":
        return {}
    if mode == "zone-only":  # the tz baseline: same zone, today's clock and the image's own tz data
        return {"TZ": c["zone"]}
    lib = faketime_lib()
    if not os.path.exists(lib):  # never the distro build: `tz` builds and probes this one
        raise SystemExit(f"futureci: {lib} missing; run `futureci.py tz` first")
    return {"TZ": c["zone"], "LD_PRELOAD": lib, "FAKETIME": "@" + c["clock"],
            "FAKETIME_DONT_FAKE_MONOTONIC": "1", "FAKETIME_DONT_RESET": "1"}


def exec_phase(out, rid, phase, cmd, cond, conds, timeout, meta=None):
    res = load_result(out, rid)
    if meta:
        res.update(meta)
    if any(p["rc"] != 0 for p in res["phases"]):
        res["phases"].append({"phase": phase, "rc": None, "outcome": "skipped", "detail": "earlier phase failed"})
        save_result(out, res)
        return 0
    env = dict(os.environ)
    junit = None
    if phase == "test":
        env.update(condition_env(cond, conds))
        junit = os.path.join(out, f"{rid}.junit.xml")
        env["PYTEST_ADDOPTS"] = (env.get("PYTEST_ADDOPTS", "") + f" --junitxml={junit} -p no:cacheprovider").strip()
    log = os.path.join(out, f"{rid}.{phase}.log")
    t0 = time.time()
    with open(log, "wb") as lf:
        p = subprocess.Popen(["bash", "-o", "pipefail", "-c", cmd], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=env, start_new_session=True)

        def pump():  # a reader thread, so a silent hang still meets the deadline
            for chunk in iter(p.stdout.readline, b""):
                lf.write(chunk)
                sys.stdout.buffer.write(chunk)
                sys.stdout.flush()
        th = threading.Thread(target=pump, daemon=True)
        th.start()
        try:
            rc = p.wait(timeout=timeout)
            outcome = "success" if rc == 0 else "failure"
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            rc, outcome = 124, "timeout"
        th.join(10)
    with open(log, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    ph = {"phase": phase, "rc": rc, "outcome": outcome, "seconds": round(time.time() - t0, 1),
          "tail": lines[-40:], "errors": error_lines(lines), "failed": failed_tests(lines)}
    if phase == "test" and cond:  # what the test process saw: proves the clock/zone took effect
        ph["clock"] = subprocess.run(["date", "+%F %T %Z %z"], env=env, capture_output=True, text=True).stdout.strip()
    if junit and os.path.exists(junit):
        ph["junit"] = junit_summary(junit)
    res["phases"].append(ph)
    save_result(out, res)
    return 0  # the verdict comes from `report`, so a failing phase never fails the job


def junit_summary(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    failed, total = [], 0
    for tc in root.iter("testcase"):
        total += 1
        bad = [c for c in tc if c.tag in ("failure", "error")]
        if bad:
            msg = (bad[0].get("message") or bad[0].text or "").strip().splitlines()
            failed.append({"id": f"{tc.get('classname', '')}::{tc.get('name', '')}", "message": (msg[0] if msg else "")[:300]})
    return {"total": total, "failed": failed}


def fetch(url, dest, sha256):
    """Download url to dest (unless already there) and refuse it unless it hashes to sha256."""
    if not os.path.exists(dest):
        for i in range(3):
            try:
                urllib.request.urlretrieve(url, dest)
                break
            except OSError:
                if i == 2:
                    raise
                time.sleep(5)
    import hashlib
    with open(dest, "rb") as f:
        h = hashlib.sha256(f.read()).hexdigest()
    if h != sha256:
        raise SystemExit(f"futureci: sha256 mismatch for {os.path.basename(dest)}: {h}")
    return dest


def faketime_lib():
    # FUTURECI_CACHE is set by the workflow before the caller's `env` applies, so a caller who sets
    # XDG_CACHE_HOME there still gets the build the tz step made
    cache = (os.environ.get("FUTURECI_CACHE") or os.environ.get("XDG_CACHE_HOME")
             or os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.join(cache, "futureci", f"libfaketime-{LIBFAKETIME['version']}-{LIBFAKETIME['build']}",
                        "libfaketime.so.1")


def faketime_install(workdir):
    """Build the pinned libfaketime into the user cache (no root needed); reuse an earlier build."""
    lib = faketime_lib()
    if os.path.exists(lib):
        return lib
    v = LIBFAKETIME["version"]
    tgz = fetch(LIBFAKETIME["url"], os.path.join(workdir, f"libfaketime-{v}.tar.gz"), LIBFAKETIME["sha256"])
    src = os.path.join(workdir, f"libfaketime-{v}")
    os.makedirs(src, exist_ok=True)
    subprocess.run(["tar", "-xzf", tgz, "-C", src, "--strip-components=1"], check=True)
    # upstream builds with -Werror; a newer compiler's warning is no reason to lose the condition, and
    # faketime_probe checks the behaviour that matters
    b = subprocess.run(["make", "-C", os.path.join(src, "src"), "libfaketime.so.1",
                        "FAKETIME_COMPILE_CFLAGS=" + LIBFAKETIME["cflags"]], capture_output=True, text=True)
    if b.returncode:
        raise SystemExit("futureci: libfaketime build failed: " + (b.stdout + b.stderr)[-600:])
    os.makedirs(os.path.dirname(lib), exist_ok=True)
    shutil.copy(os.path.join(src, "src", "libfaketime.so.1"), lib + ".tmp")
    os.replace(lib + ".tmp", lib)
    return lib


# What a test process does with time: read the clock, sleep, wait on a lock with a timeout, run a child
# process with a timeout (whose clock must carry on from the parent's, not restart at the condition's time)
# and close its inherited fds. Each one broke under some libfaketime build/setting (EINVAL, a hang, a clock
# that restarts per process, or exit 1 from stat() after the fds are closed).
FAKETIME_PROBE = r"""
import datetime, os, subprocess, sys, threading, time
time.sleep(0.05)
lock = threading.Lock(); lock.acquire(); lock.acquire(timeout=0.05)
t0 = time.time(); time.sleep(0.3)
child = subprocess.run([sys.executable, "-c", "import time; print(time.time())"], capture_output=True, text=True,
                       timeout=10)
if child.returncode or float(child.stdout) - t0 < 0.25:
    sys.exit(f"child process clock does not carry on from the parent's: {child.stdout or child.stderr!r}")
os.closerange(3, 1 << 16); os.stat("/")
print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
"""


def faketime_probe(cond, conds, timeout=30):
    """Fail unless the condition's clock is the one a test process sees and its waits still work."""
    c = conds[cond]
    env = {**os.environ, **condition_env(cond, conds)}
    try:
        p = subprocess.run([sys.executable, "-c", FAKETIME_PROBE], env=env, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"futureci: clock probe hung for {timeout} s under libfaketime")
    if p.returncode:
        raise SystemExit("futureci: clock probe failed under libfaketime: " + (p.stderr or p.stdout).strip()[-400:])
    want = dt.datetime.strptime(c["clock"], "%Y-%m-%d %H:%M:%S")
    try:
        lag = (dt.datetime.strptime(p.stdout.strip(), "%Y-%m-%d %H:%M:%S") - want).total_seconds()
    except ValueError:
        lag = None
    if lag is None or not 0 <= lag < 60:
        raise SystemExit(f"futureci: clock probe saw {p.stdout.strip()!r}, expected {c['clock']}")
    print(f"futureci: libfaketime {LIBFAKETIME['version']} probe ok ({c['zone']} {p.stdout.strip()})")


def tz_probe(c):
    pr = c["probe"]
    got = subprocess.run(["date", "-d", pr["utc"], "+%z"], env={**os.environ, "TZ": pr["zone"]},
                         capture_output=True, text=True).stdout.strip()
    return got == pr["offset"], got


def tz_install(cond, conds, workdir):
    """Make the system zoneinfo carry the condition's rules (a no-op when it already does)."""
    c = conds[cond]
    ok, got = tz_probe(c)
    if ok:
        print(f"futureci: system zoneinfo already has {c['tzdata']} rules ({c['probe']['zone']} -> {got})")
        return
    tgz = fetch(c["tzdataUrl"], os.path.join(workdir, f"tzdata{c['tzdata']}.tar.gz"), c["tzdataSha256"])
    src, zi = os.path.join(workdir, "src"), os.path.join(workdir, "zoneinfo")
    os.makedirs(src, exist_ok=True)
    subprocess.run(["tar", "-xzf", tgz, "-C", src], check=True)
    subprocess.run(["zic", "-b", "fat", "-d", zi] + [os.path.join(src, f) for f in
                   ("africa", "antarctica", "asia", "australasia", "europe", "northamerica", "southamerica",
                    "etcetera", "backward", "factory")], check=True)
    for f in ("zone1970.tab", "zone.tab", "iso3166.tab"):
        shutil.copy(os.path.join(src, f), zi)
    with open(os.path.join(zi, "+VERSION"), "w") as f:
        f.write(c["tzdata"] + "\n")
    os.symlink(".", os.path.join(zi, "posix"))
    # swap in a fresh tree: copying over the distro tree collides with its reversed link direction
    sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
    run = lambda *a: subprocess.run(sudo + list(a), check=True)
    run("rm", "-rf", "/usr/share/zoneinfo.futureci", "/usr/share/zoneinfo.orig")
    run("cp", "-a", zi, "/usr/share/zoneinfo.futureci")
    if os.path.lexists("/usr/share/zoneinfo"):
        run("mv", "/usr/share/zoneinfo", "/usr/share/zoneinfo.orig")
    run("mv", "/usr/share/zoneinfo.futureci", "/usr/share/zoneinfo")
    ok, got = tz_probe(c)
    if not ok:
        raise SystemExit(f"futureci: tz probe {c['probe']['zone']} gave {got}, expected {c['probe']['offset']}")
    print(f"futureci: system zoneinfo now {c['tzdata']} ({c['probe']['zone']} {c['probe']['utc']} -> {got})")


# ------------------------------------------------------------------ report
PHASES = ("apt", "setup", "tz", "install", "test")


def phase_failure(res):
    for ph in res.get("phases", []):
        if ph["rc"] not in (0, None) or ph["outcome"] in ("failure", "timeout"):
            return ph
    return None


def run_complete(res):
    """Final once a phase failed (later phases never run) or the test phase reported. A result file
    with only setup/install entries is an interrupted job, never a pass and never a resume skip."""
    phases = (res or {}).get("phases") or []
    return any(p["phase"] == "test" for p in phases) or (bool(phases) and phase_failure(res) is not None)


def _fails(ph):
    return f"times out at {ph['phase']}" if ph["outcome"] == "timeout" else f"fails at {ph['phase']}"


def verdict(cond_res, base_res):
    """BREAK / OK / UNKNOWN for one condition run against its baseline. Every condition run has a
    baseline; a missing one (None) is never read as a pass."""
    if cond_res is None or not cond_res.get("phases"):
        return "UNKNOWN", "condition run did not report", None
    if base_res is None or not base_res.get("phases"):
        return "UNKNOWN", "baseline run did not report", None
    if not run_complete(cond_res):
        return "UNKNOWN", "condition run was interrupted before its test phase", None
    if not run_complete(base_res):
        return "UNKNOWN", "baseline run was interrupted before its test phase", None
    bf = phase_failure(base_res)
    cf = phase_failure(cond_res)
    # a timeout depends on host load as much as on the change, and a tz failure means the condition
    # was never applied: neither is evidence that the upcoming change breaks the repo
    if cf and cf["outcome"] == "timeout":
        return "UNKNOWN", f"condition run {_fails(cf)} (load-sensitive; rerun)", cf
    if cf and cf["phase"] == "tz":
        return "UNKNOWN", "the tz condition could not be applied", cf
    if bf and not cf:
        return "UNKNOWN", f"baseline {_fails(bf)} (passes under the condition)", bf
    if bf and cf:
        if PHASES.index(cf["phase"]) < PHASES.index(bf["phase"]) and bf["outcome"] != "timeout":
            return "BREAK", f"fails at {cf['phase']}; the baseline gets past it to {bf['phase']}", cf
        if cf["phase"] == "test" and bf["phase"] == "test" and cf.get("junit") and bf.get("junit"):
            new = sorted({t["id"] for t in cf["junit"]["failed"]} - {t["id"] for t in bf["junit"]["failed"]})
            if new:
                return "BREAK", f"{len(new)} test(s) fail only under the condition", cf
        return "UNKNOWN", f"baseline already {_fails(bf)}", bf
    if not cf:
        tests = next((p.get("junit") for p in cond_res["phases"] if p.get("junit")), None)
        return "OK", (f"{tests['total']} test{'' if tests['total'] == 1 else 's'} pass" if tests else "all phases pass"), None
    if cf["phase"] == "test" and cf.get("junit"):
        bj = next((p.get("junit") for p in base_res["phases"] if p.get("junit")), None)
        if bj:
            new = sorted({t["id"] for t in cf["junit"]["failed"]} - {t["id"] for t in bj["failed"]})
            if not new and cf["junit"]["failed"]:
                return "UNKNOWN", "failing tests also fail on the baseline", cf
    return "BREAK", f"fails at {cf['phase']}", cf


def cause_lines(ph, limit=3):
    if not ph:
        return []
    if ph.get("junit") and ph["junit"]["failed"]:
        return [f"{t['id']}: {t['message']}" for t in ph["junit"]["failed"][:limit]]
    if ph.get("failed"):
        return ph["failed"][:limit]
    if ph.get("detail"):
        return [ph["detail"].strip().splitlines()[-1][:300]]
    errs = ph.get("errors") or ph.get("tail") or []
    return [e.strip() for e in errs[-limit:]]


def when(c, today):
    d = dt.date.fromisoformat(c["date"])
    days = (d - today).days
    if days > 0:
        s = f"{c['date']} (in {days} days)"
    elif days == 0:
        s = f"{c['date']} (today)"
    else:
        s = f"{c['date']} (in effect)"
    if c.get("until"):
        s += f", rollout to {c['until']}"
    return s


def report(plan_doc, results_dir, conds, today=None):
    today = today or dt.date.today()
    res = {}
    for r in plan_doc["runs"]:
        p = result_path(results_dir, r["id"])
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                res[r["id"]] = json.load(f)
    rows = []
    selected = plan_doc.get("conditions") or list(conds)
    for cid, c in sorted(conds.items(), key=lambda kv: kv[1]["date"]):
        if cid not in selected:
            continue
        static = [s for s in plan_doc["static"] if s["condition"] == cid]
        dyn = []
        for r in plan_doc["runs"]:
            if r["condition"] != cid:
                continue
            v, why, ph = verdict(res.get(r["id"]), res.get(r["baseline"]))
            dyn.append({"run": r["id"], "lang": r["lang"], "version": r["version"], "runsOn": r["runsOn"],
                        "verdict": v, "why": why, "cause": cause_lines(ph)})
        sv = [s["status"] for s in static]
        dv = [d["verdict"] for d in dyn]
        if "BREAK" in dv or ("BREAK" in sv and not all(s.get("soft") for s in static if s["status"] == "BREAK")):
            status = "BREAK"
        elif "BREAK" in sv:
            status = "BREAK (continue-on-error)"
        elif "RISK" in sv:
            status = "RISK"
        elif "UNKNOWN" in dv or ("UNKNOWN" in sv and not dv):
            status = "UNKNOWN"
        elif dv and all(v == "OK" for v in dv):
            status = "OK"
        elif "TESTED" in sv:
            status = "OK"
        elif static:
            status = "INFO"
        elif c["kind"] == "static" and dt.date.fromisoformat(c["date"]) <= today:
            status = "in effect"
        else:
            status = "not used"
        rows.append({"condition": cid, "title": c["title"], "when": when(c, today), "date": c["date"],
                     "experimental": bool(c.get("experimental")), "status": status, "static": static, "runs": dyn,
                     "fix": c["fix"], "source": c["source"]})
    skipped = [{"condition": cid, "title": c["title"], "when": when(c, today)}
               for cid, c in sorted(conds.items(), key=lambda kv: kv[1]["date"]) if cid not in selected and c.get("optIn")]
    return {"repo": plan_doc["repo"], "reportedAt": now_iso(), "rows": rows, "notChecked": skipped}


ICON = {"BREAK": "BREAK", "RISK": "RISK", "OK": "ok", "UNKNOWN": "unknown", "INFO": "info"}
MD_ACTIVE = re.compile(r"([\\*_\[\]<>|~])")


def _one_line(s, limit):
    """One printable line; a backtick could open or close a code span, so it becomes a quote."""
    s = "".join(ch for ch in " ".join(str(s).split()) if ch.isprintable()).replace("`", "'")
    return s if len(s) <= limit else s[:limit - 1] + "\u2026"


def inert(s, limit=200):
    """Repository- or test-derived text as one bounded line that Markdown shows as typed."""
    return MD_ACTIVE.sub(r"\\\1", _one_line(s, limit))


def inert_code(s, limit=300):
    """The same as a code span."""
    s = _one_line(s, limit)
    return f"`{s}`" if s.strip() else "-"


def markdown(rep):
    title = lambda r: r["title"] + (" (experimental)" if r.get("experimental") else "")
    out = ["## What will break, and when", "",
           "| When | Change | This repository | Evidence |", "|---|---|---|---|"]
    for r in rep["rows"]:
        ev = []
        for d in r["runs"]:
            ev.append(f"{d['lang']} {inert(d['version'], 40)} on {d['runsOn']}: {d['verdict']} ({inert(d['why'])})")
        n = len(r["static"])
        if n:
            ev.append(f"{n} static finding(s)")
        out.append(f"| {r['when']} | [{title(r)}]({r['source']}) | **{r['status']}** | {'; '.join(ev) or '-'} |")
    out.append("")
    if rep.get("notChecked"):
        out += ["Not checked (experimental, opt-in; add the id to `conditions`): "
                + "; ".join(f"{s['title']}, {s['when']} (`{s['condition']}`)" for s in rep["notChecked"]) + ".", ""]
    for r in rep["rows"]:
        if not (r["status"].startswith(("BREAK", "RISK", "UNKNOWN")) or r["static"]):
            continue
        out += [f"### {title(r)} — {r['status']}", f"From {r['when']}.", ""]
        for d in r["runs"]:
            if d["verdict"] != "OK":
                out.append(f"- Run {inert_code(d['run'])} ({d['lang']} {inert(d['version'], 40)}, {d['runsOn']}): "
                           f"{d['verdict']}, {inert(d['why'])}")
                out += [f"  - {inert_code(x)}" for x in d["cause"]]
        for s in r["static"][:15]:
            out.append(f"- {s['status']}: {inert(s['cause'])} — {inert_code(s['where'])}")
        if len(r["static"]) > 15:
            out.append(f"- ... {len(r['static']) - 15} more static findings in report.json")
        out += ["", f"Fix: {r['fix']}", ""]
    if rep.get("tool"):
        out += [f"Checked by future-ci at {inert_code(rep['tool'], 120)}.", ""]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ local engine
def local_run(args, conds):
    """Execute every planned run in Docker with the released setup-* actions, like a hosted job."""
    plan_doc = json.load(open(args.plan))
    os.makedirs(args.out, exist_ok=True)
    jobs = [r for r in plan_doc["runs"] if not args.only or r["id"] in args.only]
    procs, pending = [], list(jobs)
    while pending or procs:
        while pending and len(procs) < args.parallel:
            r = pending.pop(0)
            procs.append((r, subprocess.Popen(local_cmd(r, args), stdout=open(os.path.join(args.out, f"{r['id']}.docker.log"), "wb"),
                                              stderr=subprocess.STDOUT)))
        for item in list(procs):
            if item[1].poll() is not None:
                procs.remove(item)
                print(f"{item[0]['id']}: container rc={item[1].returncode}", flush=True)
        time.sleep(1)


def local_cmd(r, a):
    ver = "24.04" if r["runsOn"] == "ubuntu-24.04" else "26.04"
    setup = {"python": f"/actions/setup-python/dist/setup/index.js",
             "ruby": "/actions/setup-ruby/dist/index.js", "node": "/actions/setup-node/dist/setup/index.js"}[r["lang"]]
    inp = {"python": ["INPUT_PYTHON-VERSION=" + r["version"], "INPUT_ARCHITECTURE=x64", "INPUT_CHECK-LATEST=false",
                      "INPUT_UPDATE-ENVIRONMENT=true", "INPUT_ALLOW-PRERELEASES=false", "INPUT_FREETHREADED=false",
                      "INPUT_TOKEN=", "INPUT_CACHE=", "INPUT_PIP-INSTALL="],
           "ruby": ["INPUT_RUBY-VERSION=" + r["version"], "INPUT_BUNDLER=Gemfile.lock", "INPUT_BUNDLER-CACHE=false",
                    "INPUT_WORKING-DIRECTORY=.", "INPUT_CACHE-VERSION=0", "INPUT_SELF-HOSTED=false",
                    "INPUT_WINDOWS-TOOLCHAIN=default", "INPUT_RUBYGEMS=default", "INPUT_TOKEN="],
           "node": ["INPUT_NODE-VERSION=" + r["version"], "INPUT_ARCHITECTURE=x64", "INPUT_CHECK-LATEST=false",
                    "INPUT_TOKEN=", "INPUT_CACHE=", "INPUT_PACKAGE-MANAGER-CACHE=false", "INPUT_ALWAYS-AUTH=false",
                    "INPUT_MIRROR=", "INPUT_MIRROR-TOKEN="]}[r["lang"]]
    envs = []
    for kv in inp + [f"ImageOS=ubuntu{ver[:2]}", f"FCI_ID={r['id']}", f"FCI_COND={r['condition'] or ''}",
                     f"FCI_SETUP={setup}", f"FCI_LANG={r['lang']}", f"FCI_VERSION={r['version']}",
                     f"FCI_INSTALL={a.install}", f"FCI_TEST={a.test}", f"FCI_APT={a.apt}", f"FCI_TIMEOUT={a.timeout}"]:
        envs += ["-e", kv]
    for kv in a.env or []:
        envs += ["-e", kv]
    import hashlib
    name = f"fci-{os.getpid()}-{hashlib.sha256(os.path.abspath(a.repo).encode()).hexdigest()[:8]}-{r['id']}"
    binds = []
    if getattr(a, "workdir", None):
        # everything a job writes in bulk (checkout copy, venvs, package and build caches, toolchains,
        # the tool cache) goes to the work volume instead of the engine's writable layer, which often
        # shares a disk with the host's own services
        run_dir = os.path.join(os.path.abspath(a.workdir), "runs", os.path.basename(os.path.abspath(a.out)), r["id"])
        cache = os.path.join(os.path.abspath(a.workdir), "cache", ver)
        for sub, target in (("work", "/home/runner/work"), ("tmp", "/tmp"), ("toolcache", "/opt/hostedtoolcache"),
                            ("cargo", "/home/runner/.cargo"), ("rustup", "/home/runner/.rustup")):
            os.makedirs(os.path.join(run_dir, sub), exist_ok=True)
            if sub == "tmp":
                os.chmod(os.path.join(run_dir, sub), 0o1777)
            binds += ["-v", f"{os.path.join(run_dir, sub)}:{target}"]
        os.makedirs(cache, exist_ok=True)
        binds += ["-v", f"{cache}:/home/runner/.cache", "-e", "TMPDIR=/tmp"]
    # hosted runners raise these in configure-limits.sh; Docker's defaults (nofile 1024, stack 8 MB)
    # make socket- and thread-heavy suites fail on the baseline, which hides every verdict as UNKNOWN
    limits = ["--ulimit", "nofile=65536:65536", "--ulimit", "stack=16777216:-1"]
    return ["docker", "run", "--rm", "--name", name, "--memory", a.memory, "--cpus", str(a.cpus), *limits, *binds,
            "-v", f"{a.node}:/node:ro", "-v", f"{a.actions}:/actions:ro", "-v", f"{HERE}:/fci:ro",
            "-v", f"{os.path.abspath(a.repo)}:/src:ro", "-v", f"{os.path.abspath(a.out)}:/out",
            "-v", f"{os.path.abspath(a.tzdir)}:/tzcache:ro", *envs, f"futureci-runner:{ver}", "bash", "/fci/local/job.sh"]


# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--conditions", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--repo", default=".")
    p.add_argument("--data", default=os.path.join(HERE, "vendor", "data"))
    p.add_argument("--only", nargs="*")
    p.add_argument("--lang", choices=("python", "ruby", "node"), help="language of the test command (default: root manifest)")
    p.add_argument("--out", default=None)
    p.add_argument("--github-output", action="store_true", help="write matrix=... to $GITHUB_OUTPUT")
    e = sub.add_parser("exec")
    e.add_argument("--id", required=True)
    e.add_argument("--phase", required=True, choices=("install", "test"))
    e.add_argument("--condition", default="")
    e.add_argument("--out", required=True)
    e.add_argument("--timeout", type=int, default=1800)
    e.add_argument("--meta", default=None, help="JSON merged into the run result")
    e.add_argument("command", nargs=argparse.REMAINDER)
    r = sub.add_parser("record")
    r.add_argument("--id", required=True)
    r.add_argument("--phase", required=True)
    r.add_argument("--outcome", required=True)
    r.add_argument("--detail", default="")
    r.add_argument("--out", required=True)
    r.add_argument("--meta", default=None)
    t = sub.add_parser("tz")
    t.add_argument("--condition", default="canada-tz-2026-11-01")
    t.add_argument("--workdir", default=None)
    rp = sub.add_parser("report")
    rp.add_argument("--plan", required=True)
    rp.add_argument("--results", required=True)
    rp.add_argument("--json", default=None)
    rp.add_argument("--today", default=None)
    rp.add_argument("--tool", default=None, help="repo@commit of the tool code, named in the report")
    lo = sub.add_parser("local")
    lo.add_argument("--plan", required=True)
    lo.add_argument("--repo", required=True)
    lo.add_argument("--out", required=True)
    lo.add_argument("--install", default="")
    lo.add_argument("--test", required=True)
    lo.add_argument("--apt", default="")
    lo.add_argument("--env", action="append")
    lo.add_argument("--node", required=True, help="node 24 binary for the setup actions")
    lo.add_argument("--actions", required=True, help="dir with setup-python, setup-ruby, setup-node checkouts")
    lo.add_argument("--tzdir", required=True, help="dir holding tzdata<ver>.tar.gz")
    lo.add_argument("--only", nargs="*")
    lo.add_argument("--parallel", type=int, default=3)
    lo.add_argument("--memory", default="6g")
    lo.add_argument("--cpus", default=4)
    lo.add_argument("--workdir", default=None, help="host dir for job writes and caches (keeps them off the engine's disk)")
    lo.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args(argv)
    conds = load_conditions(a.conditions)

    if a.cmd == "plan":
        doc = plan(a.repo, conds, a.data, a.only, a.lang)
        txt = json.dumps(doc, indent=1)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(txt)
        else:
            print(txt)
        if a.github_output:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write(f"matrix={json.dumps(doc['runs'])}\n")
        return 0
    if a.cmd == "exec":
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        return exec_phase(a.out, a.id, a.phase, " ".join(cmd) if len(cmd) != 1 else cmd[0], a.condition or None,
                          conds, a.timeout, json.loads(a.meta) if a.meta else None)
    if a.cmd == "record":
        record(a.out, a.id, a.phase, a.outcome, a.detail, json.loads(a.meta) if a.meta else None)
        return 0
    if a.cmd == "tz":
        workdir = a.workdir or tempfile.mkdtemp(prefix="futureci-tz-")
        tz_install(a.condition, conds, workdir)
        faketime_install(workdir)
        faketime_probe(a.condition, conds)
        return 0
    if a.cmd == "report":
        with open(a.plan, encoding="utf-8") as f:
            doc = json.load(f)
        rep = report(doc, a.results, conds, dt.date.fromisoformat(a.today) if a.today else None)
        if a.tool:
            rep["tool"] = a.tool
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                json.dump(rep, f, indent=1)
        sys.stdout.write(markdown(rep))
        return 0
    if a.cmd == "local":
        local_run(a, conds)
        return 0


if __name__ == "__main__":
    sys.exit(main())
