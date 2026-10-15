#!/usr/bin/env python3
"""check2604.py: predict which GitHub Actions jobs fail at setup when `ubuntu-latest`
moves from Ubuntu 24.04 to 26.04 (rollout 2026-10-19 to 11-19, actions/runner-images#14748).

Covers the two setup actions that download per-OS-version builds:
  actions/setup-python  (actions/python-versions manifest, filtered by exact Ubuntu version)
  ruby/setup-ruby       (ruby-builder release assets, one per Ubuntu version)
Each step on an ubuntu-latest job (after matrix expansion) is resolved the way the action
resolves it, against the hosted 24.04 and 26.04 images, and classified:
  BREAK    installs on 24.04 today, has no build on 26.04
  OK       installs on 26.04
  ALREADY  fails on 24.04 too (not caused by the migration)
  UNKNOWN  the input cannot be resolved statically (expression, reusable workflow, ...)
Nothing is reported OK unless it was resolved.

usage: check2604.py [--data DIR] [--refresh] [--json] REPO_DIR [REPO_DIR ...]
Needs PyYAML. Data files (manifest, ruby versions, asset probe cache) live in --data.
"""
import argparse, json, os, re, sys, urllib.error, urllib.request

try:
    import tomllib
except ImportError:  # Python < 3.11: pyproject/Pipfile/mise inputs become UNKNOWN
    tomllib = None
import yaml

# Hosted image tool caches (actions/runner-images Ubuntu2404-Readme.md / Ubuntu2604-Readme.md,
# "Cached Tools", read 2026-10-12).
CACHE = {
    "24.04": {"python": ["3.10.22", "3.11.17", "3.12.15", "3.13.16", "3.14.8"],
              "ruby": ["3.2.11", "3.3.12", "3.4.11", "4.0.7"]},
    "26.04": {"python": ["3.10.21", "3.11.16", "3.12.14", "3.13.15", "3.14.7"],
              "ruby": ["3.2.11", "3.3.12", "3.4.11", "4.0.7"]},
}
OSES = ("24.04", "26.04")
PY_MANIFEST_URL = "https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json"
RUBY_VERSIONS_URL = "https://raw.githubusercontent.com/ruby/setup-ruby/master/ruby-builder-versions.json"
RUBY_ASSET_URL = "https://github.com/ruby/ruby-builder/releases/download/{e}-{v}/{e}-{v}-ubuntu-{os}-x64.tar.gz"

# ---------------------------------------------------------------- node-semver subset
# satisfies(version, range) with node-semver's default options (no includePrerelease).
# Unsupported syntax raises ValueError, which the caller reports as UNKNOWN.
_VER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_PART = re.compile(r"^[v=\s]*(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:-([0-9A-Za-z.-]+))?$")


def parse_ver(s):
    m = _VER.match(s.strip())
    if not m:
        raise ValueError("bad version " + s)
    pre = tuple(int(p) if p.isdigit() else p for p in m.group(4).split(".")) if m.group(4) else ()
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))), pre


def cmp_ver(a, b):
    if a[0] != b[0]:
        return -1 if a[0] < b[0] else 1
    pa, pb = a[1], b[1]
    if pa == pb:
        return 0
    if not pa:
        return 1
    if not pb:
        return -1
    for x, y in zip(pa, pb):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return -1 if x < y else 1
        if isinstance(x, int):
            return -1
        if isinstance(y, int):
            return 1
        return -1 if x < y else 1
    return -1 if len(pa) < len(pb) else (1 if len(pa) > len(pb) else 0)


def _wild(p):
    return p is None or p in ("x", "X", "*")


def _partial(s):
    m = _PART.match(s)
    if not m:
        raise ValueError("bad partial " + s)
    return m.group(1), m.group(2), m.group(3), m.group(4)


def _v(ma, mi, pa, pre=None):
    return ((int(ma), int(mi), int(pa)), tuple(int(p) if p.isdigit() else p for p in pre.split(".")) if pre else ())


def _comparators(tok):
    """One range token -> list of (op, version) comparators (AND)."""
    m = re.match(r"^(<=|>=|<|>|=|~>|~|\^)?\s*(.*)$", tok)
    op, rest = m.group(1) or "", m.group(2)
    ma, mi, pa, pre = _partial(rest)
    if op in ("~", "~>"):
        if _wild(ma):
            return []
        if _wild(mi):
            return [(">=", _v(ma, 0, 0)), ("<", _v(int(ma) + 1, 0, 0, "0"))]
        if _wild(pa):
            return [(">=", _v(ma, mi, 0)), ("<", _v(ma, int(mi) + 1, 0, "0"))]
        return [(">=", _v(ma, mi, pa, pre)), ("<", _v(ma, int(mi) + 1, 0, "0"))]
    if op == "^":
        if _wild(ma):
            return []
        if _wild(mi):
            return [(">=", _v(ma, 0, 0)), ("<", _v(int(ma) + 1, 0, 0, "0"))]
        if _wild(pa):
            if int(ma) == 0:
                return [(">=", _v(ma, mi, 0)), ("<", _v(ma, int(mi) + 1, 0, "0"))]
            return [(">=", _v(ma, mi, 0)), ("<", _v(int(ma) + 1, 0, 0, "0"))]
        if int(ma) == 0:
            if int(mi) == 0:
                return [(">=", _v(ma, mi, pa, pre)), ("<", _v(ma, mi, int(pa) + 1, "0"))]
            return [(">=", _v(ma, mi, pa, pre)), ("<", _v(ma, int(mi) + 1, 0, "0"))]
        return [(">=", _v(ma, mi, pa, pre)), ("<", _v(int(ma) + 1, 0, 0, "0"))]
    if op in ("", "="):
        if _wild(ma):
            return []
        if _wild(mi):
            return [(">=", _v(ma, 0, 0)), ("<", _v(int(ma) + 1, 0, 0, "0"))]
        if _wild(pa):
            return [(">=", _v(ma, mi, 0)), ("<", _v(ma, int(mi) + 1, 0, "0"))]
        return [("=", _v(ma, mi, pa, pre))]
    # <, <=, >, >= with partials
    if _wild(ma):
        return [("<", _v(0, 0, 0, "0"))] if op in ("<", ">") else []
    if _wild(mi) or _wild(pa):
        mi0 = 0 if _wild(mi) else int(mi)
        if op == ">":
            return [(">=", _v(int(ma) + 1, 0, 0) if _wild(mi) else _v(ma, mi0 + 1, 0))]
        if op == ">=":
            return [(">=", _v(ma, mi0, 0))]
        if op == "<":
            return [("<", _v(ma, mi0, 0, "0"))]
        return [("<", _v(int(ma) + 1, 0, 0, "0") if _wild(mi) else _v(ma, mi0 + 1, 0, "0"))]
    return [(op, _v(ma, mi, pa, pre))]


def parse_range(r):
    sets = []
    for alt in r.split("||"):
        alt = alt.strip()
        hm = re.match(r"^(\S+)\s+-\s+(\S+)$", alt)
        if hm:
            lo = _comparators(">=" + hm.group(1))
            ma, mi, pa, pre = _partial(hm.group(2))
            if _wild(mi):
                hi = [("<", _v(int(ma) + 1, 0, 0, "0"))]
            elif _wild(pa):
                hi = [("<", _v(ma, int(mi) + 1, 0, "0"))]
            else:
                hi = [("<=", _v(ma, mi, pa, pre))]
            sets.append(lo + hi)
            continue
        alt = re.sub(r"(<=|>=|<|>|=|~>|~|\^)\s+", r"\1", alt)
        toks = alt.split() or ["*"]
        comps = []
        for t in toks:
            comps += _comparators(t)
        sets.append(comps)
    return sets


def satisfies(version, rng):
    v = parse_ver(version)
    for comps in parse_range(rng):
        ok = True
        for op, c in comps:
            r = cmp_ver(v, c)
            if not {"=": r == 0, "<": r < 0, "<=": r <= 0, ">": r > 0, ">=": r >= 0}[op]:
                ok = False
                break
        if not ok:
            continue
        if v[1]:  # prerelease: only if some comparator in this set has a prerelease on the same tuple
            if not any(c[1] and c[0] == v[0] for _, c in comps):
                continue
        return True
    return False


# ---------------------------------------------------------------- data
class Data:
    def __init__(self, d, refresh):
        self.dir = d
        os.makedirs(d, exist_ok=True)
        self.py_manifest = json.loads(self._get("versions-manifest.json", PY_MANIFEST_URL, refresh))
        self.ruby_versions = json.loads(self._get("ruby-builder-versions.json", RUBY_VERSIONS_URL, refresh))
        self.probe_path = os.path.join(d, "ruby-assets.json")
        self.probes = {}
        if os.path.exists(self.probe_path):
            with open(self.probe_path) as f:
                self.probes = json.load(f)

    def _get(self, name, url, refresh):
        p = os.path.join(self.dir, name)
        if refresh or not os.path.exists(p):
            with urllib.request.urlopen(url, timeout=60) as r, open(p, "wb") as f:
                f.write(r.read())
        with open(p, encoding="utf-8") as f:
            return f.read()

    def ruby_asset(self, engine, version, osv):
        key = f"{engine}-{version}-{osv}"
        if key not in self.probes:
            url = RUBY_ASSET_URL.format(e=engine, v=version, os=osv)
            req = urllib.request.Request(url, method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.probes[key] = r.status
            except urllib.error.HTTPError as e:
                self.probes[key] = e.code
            except OSError:
                return None
            with open(self.probe_path, "w") as f:
                json.dump(self.probes, f, indent=0, sort_keys=True)
        return self.probes[key]


# ---------------------------------------------------------------- resolution
class Unknown(Exception):
    pass


def py_semantic(spec, allow_pre):
    """setup-python desugarVersion + pythonVersionToSemantic. Returns (range, freethreaded)."""
    ft = False
    m = re.match(r"^(\d+\.\d+(\.\d+)?)t$", spec)
    if m:
        spec, ft = m.group(1), True
    m = re.match(r"^(\d+\.\d+)t(-dev)$", spec)
    if m:
        spec, ft = m.group(1) + m.group(2), True
    spec = re.sub(r"^(\d+)\.(\d+)-dev$", r"~\1.\2.0-0", spec)
    spec = re.sub(r"(\d+\.\d+\.\d+)((?:a|b|rc)\d*)", r"\1-\2", spec)
    if allow_pre:
        spec = re.sub(r"^(\d+)\.(\d+)$", r"~\1.\2.0-0", spec)
    return spec, ft


def resolve_python(data, spec, osv, arch="x64", allow_pre=False, freethreaded=False):
    """Return the version setup-python would install on hosted ubuntu-<osv>, or None."""
    rng, ft = py_semantic(spec, allow_pre)
    ft = ft or freethreaded
    a = arch + ("-freethreaded" if ft else "")
    try:
        parse_range(rng)
    except ValueError:
        raise Unknown(f"unsupported version syntax {spec!r}")
    if a == "x64":  # hosted cache holds x64 non-freethreaded builds
        hits = [v for v in CACHE[osv]["python"] if satisfies(v, rng)]
        if hits:
            return max(hits, key=parse_ver)
    for rel in data.py_manifest:  # manifest is newest-first; first match wins
        try:
            if not satisfies(rel["version"], rng):
                continue
        except ValueError:
            continue
        for f in rel["files"]:
            if f.get("platform") == "linux" and f.get("arch") == a and f.get("platform_version") == osv:
                return rel["version"]
    return None


def _stable_ruby(engine, v):
    if engine == "ruby":
        return re.fullmatch(r"\d+(\.\d+)*", v) is not None
    return re.fullmatch(r"\d+(\.\d+)*", v) is not None and "preview" not in v and "rc" not in v


def resolve_ruby(data, spec, osv):
    """setup-ruby parseRubyEngineAndVersion + validateRubyEngineAndVersion + download."""
    if spec in ("head", "debug") or spec.endswith("-head") or spec.endswith("-dev"):
        raise Unknown(f"head build {spec!r} (separate asset repository)")
    if re.match(r"^\d", spec):
        engine, ver = "ruby", spec
    elif "-" not in spec:
        engine, ver = spec, ""
    else:
        engine, ver = spec.split("-", 1)
    versions = data.ruby_versions.get(engine)
    if versions is None:
        raise Unknown(f"engine {engine!r} not in setup-ruby's version list")
    if ver not in versions:
        rev = list(reversed(versions))
        found = next((v for v in rev if _stable_ruby(engine, v) and v.startswith(ver)), None)
        found = found or next((v for v in rev if v not in ("head", "debug") and v.startswith(ver)), None)
        if not found:
            return engine, None, None  # "Unknown version" error on every OS
        ver = found
    if engine == "ruby" and ver in CACHE[osv]["ruby"]:
        return engine, ver, 200
    return engine, ver, data.ruby_asset(engine, ver, osv)


# ---------------------------------------------------------------- workflow reading
def js_str(v):
    """How the runner stringifies a YAML scalar input (3.10 -> '3.1', 3.0 -> '3')."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


_SEG = re.compile(r"\.([A-Za-z0-9_-]+)|\[(\d+)\]|\[['\"]([^'\"]+)['\"]\]")


_PATH = re.compile(r"(matrix|env)((?:\.[A-Za-z0-9_-]+|\[\d+\]|\[['\"][^'\"]+['\"]\])+)")


def lookup(e, ctx):
    """Value of a matrix./env. path such as matrix.os.image or matrix.plat[0]; Unknown otherwise."""
    m = _PATH.fullmatch(e.strip())
    if not m:
        raise Unknown(f"expression ${{{{ {e} }}}}")
    v = ctx[m.group(1)]
    for seg in _SEG.finditer(m.group(2)):
        key, idx, qkey = seg.groups()
        if idx is not None:
            if not isinstance(v, list) or int(idx) >= len(v):
                raise Unknown(f"expression ${{{{ {e} }}}}")
            v = v[int(idx)]
        else:
            k = key if key is not None else qkey
            if isinstance(v, dict) and k not in v:  # property names are case-insensitive
                k = next((x for x in v if isinstance(x, str) and x.lower() == k.lower()), k)
            if not isinstance(v, dict) or k not in v:
                raise Unknown(f"expression ${{{{ {e} }}}}")
            v = v[k]
    return v


_LIT = re.compile(r"'((?:[^']|'')*)'")


def fallback(e, ctx):
    """`matrix.os || 'ubuntu-latest'`: the first truthy operand, else the last one, as in the runner.
    Operands are matrix./env. paths or string literals; a matrix value missing from this combination
    is null. Any other operand (&&, !, ==, calls, inputs., vars.) leaves the expression Unknown."""
    v = None
    for p in (x.strip() for x in e.split("||")):  # && and comparisons bind tighter than ||
        lit = _LIT.fullmatch(p)
        if lit:
            v = lit.group(1).replace("''", "'")
        elif _PATH.fullmatch(p):
            try:
                v = lookup(p, ctx)
            except Unknown:
                if not p.startswith("matrix"):
                    raise
                v = None
        else:
            raise Unknown(f"expression ${{{{ {e} }}}}")
        if v is not None and v is not False and v != 0 and v != "":
            return v
    return "" if v is None else v


def substitute(val, ctx):
    """Replace ${{ matrix.x }} / ${{ env.X }} / ${{ a || 'b' }}; raise Unknown on anything else."""
    if isinstance(val, (list, dict)):
        raise Unknown("non-scalar input")
    s = js_str(val)

    def rep(m):
        v = fallback(m.group(1), ctx) if "||" in m.group(1) else lookup(m.group(1), ctx)
        if isinstance(v, (list, dict)):
            raise Unknown(f"non-scalar {m.group(1)}")
        return js_str(v)

    for _ in range(3):  # env values may themselves reference matrix values
        if not EXPR.search(s):
            return s
        s = EXPR.sub(rep, s)
    raise Unknown("nested expression")


def expand_matrix(strategy):
    """GitHub matrix semantics: product, exclude (partial match), include (extend or add)."""
    if not strategy or "matrix" not in strategy:
        return [{}]
    m = strategy["matrix"]
    if isinstance(m, str):
        raise Unknown("matrix is an expression")
    base = {k: v for k, v in m.items() if k not in ("include", "exclude")}
    for k, v in base.items():
        if isinstance(v, str) and EXPR.search(v):
            raise Unknown(f"matrix.{k} is an expression")
        if not isinstance(v, list):
            raise Unknown(f"matrix.{k} is not a list")
    combos = [{}]
    for k, vals in base.items():
        combos = [dict(c, **{k: v}) for c in combos for v in vals]
    if not base:
        combos = []
    excl = m.get("exclude") or []
    inc = m.get("include") or []
    if isinstance(excl, str) or isinstance(inc, str):
        raise Unknown("include/exclude is an expression")
    combos = [c for c in combos if not any(all(c.get(k) == v for k, v in e.items()) for e in excl)]
    originals = [dict(c) for c in combos]  # include entries never extend combinations added by include
    for e in inc:
        applied = False
        for c, orig in zip(combos, originals):
            if all(orig.get(k, v) == v for k, v in e.items() if k in base):
                c.update(e)
                applied = True
        if not applied:
            combos.append(dict(e))
    return combos or [{}]


def hosted_ubuntu_latest(runs_on):
    """True if runs-on selects the hosted ubuntu-latest label; Unknown if not static."""
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels")
        if labels is None:
            return False  # runner group: larger/self-hosted runners
        runs_on = labels
    if isinstance(runs_on, list):
        if any(isinstance(x, str) and EXPR.search(x) for x in runs_on):
            raise Unknown("runs-on expression")
        return runs_on == ["ubuntu-latest"]
    if isinstance(runs_on, str):
        if EXPR.search(runs_on):
            raise Unknown("runs-on expression")
        return runs_on.strip() == "ubuntu-latest"
    return False


COND = re.compile(r"^\s*(matrix(?:\.[A-Za-z0-9_-]+|\[\d+\]|\['[^']+'\])+|runner\.os)\s*(==|!=)\s*'([^']*)'\s*$")


def step_runs(cond, ctx):
    """Evaluate simple `if:` conditions; True when unconditioned or not matrix/OS related."""
    if cond is None:
        return True
    c = js_str(cond).strip()
    c = EXPR.fullmatch(c).group(1) if EXPR.fullmatch(c) else c
    if "matrix." not in c and "runner.os" not in c:
        return True
    for p in re.split(r"\s*&&\s*", c):
        if "matrix." not in p and "runner.os" not in p:
            continue  # unrelated to the runner (event, branch, ...): assume it can run
        mm = COND.match(p.strip("() "))
        if not mm:
            raise Unknown(f"condition {c!r}")
        if mm.group(1) == "runner.os":
            left = "Linux"
        else:
            try:
                left = lookup(mm.group(1), ctx)
            except Unknown:
                left = None  # a missing matrix key compares as empty
        left = js_str(left) if left is not None else ""
        eq = left.lower() == mm.group(3).lower()  # expression string comparison ignores case
        if (mm.group(2) == "==") != eq:
            return False
    return True


def read_text(root, rel):
    """A file inside the checkout; None if missing or if it (or a symlink) leaves the checkout."""
    base = os.path.realpath(root)
    p = os.path.realpath(os.path.join(base, rel))
    if os.path.commonpath([base, p]) != base or not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(65536)
    except OSError:
        return None


def py_specs_from_file(root, rel):
    """setup-python getVersionInputFromFile."""
    txt = read_text(root, rel)
    if txt is None:
        raise Unknown(f"python-version-file {rel} not in checkout")
    if rel.endswith(".toml"):
        if tomllib is None:
            raise Unknown("tomllib unavailable")
        try:
            cfg = tomllib.loads(txt)
        except Exception:
            raise Unknown(f"{rel} not parseable")
        v = (cfg.get("project", {}).get("requires-python") if "project" in cfg
             else cfg.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python"))
        if v is None:
            return []
        return [" ".join(v.split(","))]
    if ".tool-versions" in rel:
        for line in txt.split("\n"):
            if line.strip().startswith("#"):
                continue
            m = re.match(r"^\s*python\s*v?\s*(\S+)\s*$", line)
            if m:
                return [m.group(1)]
        return []
    if "Pipfile" in rel:
        if tomllib is None:
            raise Unknown("tomllib unavailable")
        try:
            req = tomllib.loads(txt).get("requires", {})
        except Exception:
            raise Unknown(f"{rel} not parseable")
        v = req.get("python_full_version", req.get("python_version"))
        return [v] if v else []
    out = []
    for line in txt.strip().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        out.append(line.strip().split("/")[0])
    return out


def ruby_spec_default(root, wd, names=(".ruby-version", ".tool-versions", "mise.toml")):
    for name in names:
        txt = read_text(root, os.path.join(wd, name))
        if txt is None:
            continue
        txt = txt.strip()
        if name == ".ruby-version":
            return txt
        rx = r"^ruby\s+(\S+)" if name == ".tool-versions" else r"""^\s*ruby\s*=\s*['"]([^'"]+)['"]\s*(?:#.*)?$"""
        for line in txt.splitlines():
            m = re.match(rx, line)
            if m:
                return m.group(1)
        raise Unknown(f"{name} has no ruby line")
    raise Unknown("no ruby-version and no version file")


def classify(ok24, ok26):
    if ok26:
        return "OK"
    return "BREAK" if ok24 else "ALREADY"


def check_step(data, root, step, ctx):
    uses = step.get("uses", "")
    w = step.get("with") or {}
    action = uses.split("@")[0].lower()
    out = []
    if action == "actions/setup-python":
        arch = substitute(w.get("architecture", "x64"), ctx) or "x64"
        allow = substitute(w.get("allow-prereleases", "false"), ctx) == "true"
        ftin = substitute(w.get("freethreaded", "false"), ctx) == "true"
        if "python-version" in w and w["python-version"] not in (None, ""):
            raw = substitute(w["python-version"], ctx)
            specs = [s.strip() for s in raw.splitlines() if s.strip()]
            src = "python-version"
        elif w.get("python-version-file"):
            rel = substitute(w["python-version-file"], ctx)
            specs, src = py_specs_from_file(root, rel), rel
        else:
            if read_text(root, ".python-version") is None:
                return [("NA", "setup-python", "", "no version: uses python from PATH")]
            specs, src = py_specs_from_file(root, ".python-version"), ".python-version"
        for s in specs:
            if s.startswith(("pypy", "graalpy")):
                out.append(("NA", "setup-python", s, "PyPy/GraalPy builds are not per Ubuntu version"))
                continue
            r24 = resolve_python(data, s, "24.04", arch, allow, ftin)
            r26 = resolve_python(data, s, "26.04", arch, allow, ftin)
            out.append((classify(r24, r26), "setup-python", s, f"{src}: 24.04->{r24} 26.04->{r26}"))
    elif action == "ruby/setup-ruby":
        wd = substitute(w.get("working-directory", "."), ctx)
        raw = substitute(w.get("ruby-version", "default"), ctx)
        if raw == "default":
            spec, src = ruby_spec_default(root, wd), "version file"
        elif raw in (".ruby-version", ".tool-versions", "mise.toml"):
            spec, src = ruby_spec_default(root, wd, (raw,)), raw
        else:
            spec, src = raw, "ruby-version"
        e, v24, s24 = resolve_ruby(data, spec, "24.04")
        _, v26, s26 = resolve_ruby(data, spec, "26.04")
        if v24 is None:  # version resolution is the same on every OS
            return [("ALREADY", "setup-ruby", spec, "unknown version on every OS")]
        if s24 is None or s26 is None:
            raise Unknown("asset probe failed")
        out.append((classify(s24 == 200, s26 == 200), "setup-ruby", spec, f"{src}: {e}-{v26} 24.04 HTTP {s24}, 26.04 HTTP {s26}"))
    return out


def check_workflow(data, root, path, doc):
    res = []
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        return res
    wenv = doc.get("env") if isinstance(doc.get("env"), dict) else {}
    for jid, job in doc["jobs"].items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps") or []
        relevant = [s for s in steps if isinstance(s, dict) and s.get("uses", "").split("@")[0].lower()
                    in ("actions/setup-python", "ruby/setup-ruby")]
        if "uses" in job and not steps:
            continue  # reusable workflow call: checked where defined (inputs there are UNKNOWN)
        if not relevant:
            continue
        loc = f"{path}:{jid}"
        try:
            combos = expand_matrix(job.get("strategy"))
        except Unknown as u:
            res.append(("UNKNOWN", "", "", f"{loc}: {u}", False))
            continue
        jenv = job.get("env") if isinstance(job.get("env"), dict) else {}
        soft_job = job.get("continue-on-error")
        for combo in combos:
            ctx = {"matrix": combo, "env": {}}
            label = ",".join(f"{k}={js_str(v)}" for k, v in combo.items() if not isinstance(v, (dict, list)))
            where = f"{loc}[{label}]" if label else loc
            try:
                ro = job.get("runs-on")
                if isinstance(ro, str) and EXPR.search(ro):
                    ro = substitute(ro, ctx)
                elif isinstance(ro, list):
                    ro = [substitute(x, ctx) if isinstance(x, str) else x for x in ro]
                if not hosted_ubuntu_latest(ro):
                    continue
                if job.get("container"):
                    res.append(("NA", "", "", f"{where}: runs in a container", False))
                    continue
                if not step_runs(job.get("if"), ctx):
                    continue
                try:
                    soft = soft_job is True or (isinstance(soft_job, str) and substitute(soft_job, ctx) == "true")
                except Unknown:
                    soft = None  # continue-on-error not static: the setup result is still known
                ctx["env"] = {**{k: v for k, v in wenv.items()}, **jenv}
                for s in relevant:
                    senv = s.get("env") if isinstance(s.get("env"), dict) else {}
                    sctx = {"matrix": combo, "env": {**ctx["env"], **senv}}
                    try:
                        if not step_runs(s.get("if"), sctx):
                            continue
                        for st, act, spec, why in check_step(data, root, s, sctx):
                            res.append((st, act, spec, f"{where}: {why}", soft))
                    except Unknown as u:
                        res.append(("UNKNOWN", s.get("uses", ""), "", f"{where}: {u}", soft))
            except Unknown as u:
                res.append(("UNKNOWN", "", "", f"{where}: {u}", False))
    return res


def check_repo(data, root):
    wfdir = os.path.join(root, ".github", "workflows")
    out = []
    if not os.path.isdir(wfdir):
        return out
    for name in sorted(os.listdir(wfdir)):
        if not name.endswith((".yml", ".yaml")):
            continue
        p = os.path.join(wfdir, name)
        if os.path.islink(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                doc = yaml.safe_load(f)
        except Exception as e:
            out.append(("UNKNOWN", "", "", f".github/workflows/{name}: YAML error {type(e).__name__}", False))
            continue
        out += check_workflow(data, root, f".github/workflows/{name}", doc)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repos", nargs="+")
    ap.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    ap.add_argument("--refresh", action="store_true", help="re-download manifests")
    ap.add_argument("--json", action="store_true", help="one JSON object per repo")
    a = ap.parse_args(argv)
    data = Data(a.data, a.refresh)
    worst = 0
    for r in a.repos:
        res = check_repo(data, r)
        if a.json:
            print(json.dumps({"repo": r, "findings": [dict(zip(("status", "action", "spec", "where", "continueOnError"), x)) for x in res]}))
        else:
            for st, act, spec, where, soft in res:
                if st != "NA":
                    print(f"{st:8} {act or '-':13} {spec or '-':10} {where}{' (continue-on-error)' if soft else ''}")
        if any(x[0] == "BREAK" for x in res):
            worst = 1
    return worst


if __name__ == "__main__":
    sys.exit(main())
