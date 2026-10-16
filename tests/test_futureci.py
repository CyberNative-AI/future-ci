import hashlib, json, os, re, subprocess, sys, tempfile, textwrap, unittest, unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import futureci as F

CONDS = F.load_conditions()


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(textwrap.dedent(text))


def run(phases):
    return {"id": "x", "phases": phases}


OKP = [{"phase": "setup", "rc": 0, "outcome": "success"}, {"phase": "install", "rc": 0, "outcome": "success"},
       {"phase": "test", "rc": 0, "outcome": "success"}]


def failing_test(ids, total=10):
    return OKP[:2] + [{"phase": "test", "rc": 1, "outcome": "failure",
                       "junit": {"total": total, "failed": [{"id": i, "message": "boom"} for i in ids]}}]


class SpecTests(unittest.TestCase):
    def test_caps(self):
        cases = [(">=3.10,<3.15", "3.14.0", True), (">=3.10,<3.15", "3.15.0", False), (">=3.8", "3.15.0", True),
                 ("!=3.15.*", "3.15.0", False), ("!=3.15.*", "3.14.2", True), ("~=3.10", "3.15.0", True),
                 ("~=3.10.0", "3.15.0", False), ("<3.15.1", "3.15.0", True), ("<=3.14", "3.15.0", False),
                 ("==3.*", "3.15.0", True), (">=3.9, <4", "3.15.0", True), ("python3", "3.15.0", None)]
        for spec, v, want in cases:
            self.assertEqual(F.spec_allows(spec, v), want, (spec, v))

    def test_full_pep440_versions_in_specifiers(self):
        # pip compares whole PEP 440 versions: 3.15.0 is above 3.15rc1 and below 3.15.0.1
        cases = [("<3.15rc1", False), (">=3.15.0.1", False), ("<3.15.0.1", True), (">3.15.0.dev1", True),
                 ("<3.15.0.post1", True), (">3.15.0", False), ("==3.15.0.0", True), ("==3.15.0.*", True),
                 ("==1!3.15.*", False), ("===3.15.0", True), ("===3.15", False), ("==3.15.0+local", False),
                 ("!=3.15.0+local", True), ("~=3.15", True), ("~=3.14.0", False), (">=3.10,", True), (">= 3.10 , < 4", True)]
        for spec, want in cases:
            self.assertEqual(F.spec_allows(spec, "3.15.0"), want, spec)
        # invalid for pip, or a ~= whose prefix pip takes from the spelling: undecided, never a guess
        for spec in (">=3.5.*", "<=3.15+local", "<3.15;", "=3.15", "~=3", "~=3.15rc1", "~=V3.4", ">=3.\u0661\u0665"):
            self.assertIsNone(F.spec_allows(spec, "3.15.0"), spec)


class StaticTests(unittest.TestCase):
    def test_removed_imports(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "pkg/a.py", "import sre_constants\nfrom sre_parse import parse\n")
            write(d, "pkg/b.py", "try:\n    import sre_compile\nexcept ImportError:\n    sre_compile = None\n")
            write(d, "pkg/c.py", "from . import sre_parse\nx = 'import sre_parse'\n")
            write(d, "node_modules/z.py", "import sre_parse\n")
            hits = sorted(F.removed_imports(d))
            self.assertEqual(hits, [("pkg/a.py", 1, "sre_constants", False), ("pkg/a.py", 2, "sre_parse", False),
                                    ("pkg/b.py", 2, "sre_compile", True)])

    def test_plan_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, ".github/workflows/ci.yml", """
                on: push
                jobs:
                  test:
                    runs-on: ubuntu-latest
                    strategy:
                      matrix:
                        python-version: ["3.9", "3.13"]
                    steps:
                      - uses: actions/setup-python@v6
                        with:
                          python-version: ${{ matrix.python-version }}
                  mac:
                    runs-on: ${{ matrix.os }}
                    strategy:
                      matrix:
                        os: [macos-14, macos-15]
                    steps:
                      - run: true
                """)
            write(d, "pyproject.toml", '[project]\nname="x"\nrequires-python = ">=3.9,<3.15"\ndependencies=["modal>=1", "pyyaml"]\n')
            write(d, "tests/test_t.py", "TZ = 'America/Winnipeg'\n")
            data = os.path.join(os.path.dirname(HERE), "vendor", "data")
            offline = unittest.mock.patch.dict(os.environ, {"FUTURECI_PYPI": "off"})  # the snapshot, not today's PyPI
            offline.start()
            self.addCleanup(offline.stop)
            p = F.plan(d, CONDS, data)  # the default set leaves out the experimental opt-in conditions
            self.assertEqual(p["conditions"], ["ubuntu-26.04", "python-3.15", "macos-14-retired", "node24-actions"])
            self.assertFalse(any(r["condition"] and r["condition"].startswith("canada-tz") for r in p["runs"]))
            self.assertFalse(any(s["condition"] == "canada-tz-2026-11-01" for s in p["static"]))
            p = F.plan(d, CONDS, data, ["default", "canada-tz-2026-11-01"])
            self.assertEqual(p["conditions"][-1], "canada-tz-2026-11-01")
            st = {(s["condition"], s["status"], s["cause"].split(":")[0]) for s in p["static"]}
            self.assertIn(("ubuntu-26.04", "BREAK", "setup-python 3.9"), st)
            self.assertIn(("macos-14-retired", "BREAK", "job runs on macos-14, which stops running on the retirement date"), st)
            self.assertTrue(any(c == "python-3.15" and s == "BREAK" and "requires-python" in w for c, s, w in st))
            self.assertTrue(any(c == "python-3.15" and s == "RISK" and w.startswith("modal") for c, s, w in st))
            self.assertTrue(all(s["cause"].endswith(f"(PyPI not read; snapshot of {F.PY315_SNAPSHOT})")
                                for s in p["static"] if s["condition"] == "python-3.15" and s["status"] == "RISK"))
            self.assertTrue(any(c == "python-3.15" and s == "RISK" and w.startswith("pyyaml") for c, s, w in st))
            self.assertIn(("canada-tz-2026-11-01", "INFO", "code or tests name an affected Canadian zone"), st)
            ids = {r["id"]: r for r in p["runs"]}
            self.assertEqual(ids["u2604-python-oldest"]["version"], "3.9")
            self.assertEqual(ids["u2604-python-oldest"]["baseline"], "base-python-oldest")
            self.assertEqual(ids["py315"]["baseline"], "base-python-newest")
            self.assertEqual(ids["tz-python"]["baseline"], "tzbase-python")
            self.assertEqual(ids["tzbase-python"]["condition"], "canada-tz-2026-11-01:zone-only")
            self.assertNotIn("node26", ids)

    def test_plan_already_on_315_and_lang(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, ".github/workflows/ci.yml", """
                on: push
                jobs:
                  t:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/setup-python@v6
                        with: {python-version: "3.15"}
                      - uses: actions/setup-node@v6
                        with: {node-version: "22"}
                """)
            write(d, "setup.py", "from setuptools import setup\nsetup(name='x')\n")
            p = F.plan(d, CONDS, os.path.join(os.path.dirname(HERE), "vendor", "data"))
            ids = [r["id"] for r in p["runs"]]
            self.assertNotIn("py315", ids)
            self.assertFalse(any(i.endswith("node") or i == "node26" for i in ids))  # tests are Python
            self.assertTrue(any(s["status"] == "TESTED" for s in p["static"]))
            p = F.plan(d, CONDS, os.path.join(os.path.dirname(HERE), "vendor", "data"), test_lang="node")
            self.assertEqual(p["runs"], [])  # no default condition runs Node tests, so no baseline either
            p = F.plan(d, CONDS, os.path.join(os.path.dirname(HERE), "vendor", "data"),
                       ["node-26-lts", "canada-tz-2026-11-01"], test_lang="node")
            self.assertEqual([r["id"] for r in p["runs"] if "node" in r["id"]], ["base-node", "node26", "tzbase-node", "tz-node"])

    def test_ok_counts_tests(self):
        one = OKP[:2] + [{"phase": "test", "rc": 0, "outcome": "success", "junit": {"total": 1, "failed": []}}]
        self.assertEqual(F.verdict(run(one), run(OKP))[1], "1 test pass")
        one[2]["junit"]["total"] = 12
        self.assertEqual(F.verdict(run(one), run(OKP))[1], "12 tests pass")

    def test_select_conditions(self):
        self.assertEqual(F.select_conditions(CONDS, ["python-3.15"]), ["python-3.15"])
        self.assertEqual(F.select_conditions(CONDS, ["node-26-lts", "default", "python-3.15"]),
                         ["node-26-lts", "ubuntu-26.04", "python-3.15", "macos-14-retired", "node24-actions"])
        with self.assertRaises(SystemExit) as e:
            F.select_conditions(CONDS, ["default", "python-3.16"])
        self.assertIn("python-3.16", str(e.exception))

    def test_unused_baselines_are_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "pyproject.toml", '[project]\nname="x"\n')
            p = F.plan(d, CONDS, os.path.join(os.path.dirname(HERE), "vendor", "data"), ["python-3.15"])
            self.assertEqual([(r["id"], r["baseline"]) for r in p["runs"]], [("base-python-oldest", None),
                                                                             ("py315", "base-python-oldest")])


class VerdictTests(unittest.TestCase):
    def test_matrix(self):
        setup_fail = [{"phase": "setup", "rc": 1, "outcome": "failure", "detail": "not found for ubuntu 26.04"}]
        timeout = {"phase": "install", "rc": 124, "outcome": "timeout"}
        tz_fail = {"phase": "tz", "rc": 1, "outcome": "failure"}
        cases = [
            (run(OKP), run(OKP), "OK"),
            (run(setup_fail), run(OKP), "BREAK"),
            (run(OKP), run(setup_fail), "UNKNOWN"),                        # red baseline is never a BREAK
            (run(setup_fail), run(setup_fail), "UNKNOWN"),
            (run(failing_test(["a"])), run(failing_test(["a"])), "UNKNOWN"),  # same failures as the baseline
            (run(failing_test(["a", "b"])), run(failing_test(["a"])), "BREAK"),  # one new failing test
            (run(failing_test(["a"])), run(OKP), "BREAK"),
            (None, run(OKP), "UNKNOWN"),
            (run(OKP), {"id": "b", "phases": []}, "UNKNOWN"),
            (run(setup_fail), run(failing_test(["a"])), "BREAK"),          # breaks before the baseline's failure
            (run(OKP[:1]), run(OKP), "UNKNOWN"),                           # interrupted after setup is not a pass
            (run(OKP), run(OKP[:2]), "UNKNOWN"),                           # interrupted baseline
            (run(OKP[:1] + [timeout]), run(OKP), "UNKNOWN"),               # timeouts are load-sensitive
            (run(setup_fail), run(OKP[:1] + [timeout]), "UNKNOWN"),        # earlier than a timed-out baseline
            (run(OKP[:1] + [tz_fail]), run(OKP), "UNKNOWN"),               # condition never applied
        ]
        for cond, base, want in cases:
            self.assertEqual(F.verdict(cond, base)[0], want, (cond, base))

    def test_report_rows(self):
        with tempfile.TemporaryDirectory() as d:
            plan = {"repo": d, "static": [], "runs": [
                {"id": "b", "condition": None, "runsOn": "ubuntu-24.04", "lang": "python", "version": "3.9", "baseline": None},
                {"id": "u", "condition": "ubuntu-26.04", "runsOn": "ubuntu-26.04", "lang": "python", "version": "3.9", "baseline": "b"}]}
            F.save_result(d, {"id": "b", "phases": OKP})
            F.save_result(d, {"id": "u", "phases": [{"phase": "setup", "rc": 1, "outcome": "failure", "detail": "x\nThe version '3.9' was not found"}]})
            rep = F.report(plan, d, CONDS, F.dt.date(2026, 10, 14))
            row = next(r for r in rep["rows"] if r["condition"] == "ubuntu-26.04")
            self.assertEqual(row["status"], "BREAK")
            self.assertEqual(row["when"], "2026-10-19 (in 5 days), rollout to 2026-11-19")
            md = F.markdown(rep)
            self.assertIn("The version '3.9' was not found", md)
            self.assertEqual(next(r for r in rep["rows"] if r["condition"] == "node24-actions")["status"], "in effect")
            # a plan from before condition selection reports every condition
            self.assertIn("canada-tz-2026-11-01", [r["condition"] for r in rep["rows"]])
            plan["conditions"] = ["ubuntu-26.04", "python-3.15", "macos-14-retired", "node24-actions"]
            rep = F.report(plan, d, CONDS, F.dt.date(2026, 10, 14))
            self.assertEqual([r["condition"] for r in rep["rows"]],
                             ["node24-actions", "python-3.15", "ubuntu-26.04", "macos-14-retired"])
            self.assertEqual([s["condition"] for s in rep["notChecked"]], ["node-26-lts", "canada-tz-2026-11-01"])
            md = F.markdown(rep)
            self.assertIn("Not checked (experimental, opt-in", md)
            self.assertIn("(`canada-tz-2026-11-01`)", md)
            plan["conditions"].append("canada-tz-2026-11-01")
            md = F.markdown(F.report(plan, d, CONDS, F.dt.date(2026, 10, 14)))
            self.assertIn("the US falls back the same day (experimental)](", md)


    def test_run_complete(self):
        self.assertFalse(F.run_complete(run(OKP[:1])))
        self.assertFalse(F.run_complete(run(OKP[:2])))
        self.assertFalse(F.run_complete(None))
        self.assertTrue(F.run_complete(run(OKP)))
        self.assertTrue(F.run_complete(run([{"phase": "setup", "rc": 1, "outcome": "failure"}])))
        self.assertTrue(F.run_complete(run(OKP[:1] + [{"phase": "install", "rc": 124, "outcome": "timeout"},
                                                       {"phase": "test", "rc": None, "outcome": "skipped"}])))

    def test_missing_baseline_is_unknown(self):
        """A planned baseline whose result is absent gives UNKNOWN, for passing and failing condition
        runs alike; with the baseline present the same results grade OK and BREAK; static findings stay."""
        with tempfile.TemporaryDirectory() as d:
            plan = {"repo": d, "conditions": ["ubuntu-26.04", "python-3.15"], "static": [], "runs": [
                {"id": "b", "condition": None, "runsOn": "ubuntu-24.04", "lang": "python", "version": "3.14", "baseline": None},
                {"id": "u", "condition": "ubuntu-26.04", "runsOn": "ubuntu-26.04", "lang": "python", "version": "3.14", "baseline": "b"},
                {"id": "p", "condition": "python-3.15", "runsOn": "ubuntu-24.04", "lang": "python", "version": "3.15", "baseline": "b"}]}
            F.save_result(d, {"id": "b", "phases": OKP})
            F.save_result(d, {"id": "u", "phases": OKP})
            F.save_result(d, {"id": "p", "phases": failing_test(["t::a"])})
            verdicts = lambda: {x["run"]: (x["verdict"], x["why"]) for r in F.report(plan, d, CONDS)["rows"] for x in r["runs"]}
            self.assertEqual({k: v[0] for k, v in verdicts().items()}, {"u": "OK", "p": "BREAK"})
            os.remove(F.result_path(d, "b"))
            self.assertEqual(verdicts(), {"u": ("UNKNOWN", "baseline run did not report"),
                                          "p": ("UNKNOWN", "baseline run did not report")})
            plan["static"] = [{"condition": "python-3.15", "status": "BREAK", "where": "pyproject.toml",
                               "cause": "requires-python <3.15"}]
            rows = {r["condition"]: r["status"] for r in F.report(plan, d, CONDS)["rows"]}
            self.assertEqual((rows["python-3.15"], rows["ubuntu-26.04"]), ("BREAK", "UNKNOWN"))
        self.assertEqual(F.verdict(run(OKP), None)[0], "UNKNOWN")
        self.assertEqual(F.verdict(run(failing_test(["a"])), None)[0], "UNKNOWN")

    def test_report_text_from_tests_is_inert(self):
        """Test names, messages and versions cannot add lines, headings, links, HTML or table cells."""
        hostile = "x\n\n# Owned\n<img src=x onerror=alert(1)> [click](https://e.example) | a | b ` *b* _i_ \x1b[31m"
        with tempfile.TemporaryDirectory() as d:
            plan = {"repo": d, "conditions": ["python-3.15"], "runs": [
                {"id": "b", "condition": None, "runsOn": "ubuntu-24.04", "lang": "python", "version": "3.14", "baseline": None},
                {"id": "p", "condition": "python-3.15", "runsOn": "ubuntu-24.04", "lang": "python", "version": "3.15" + hostile, "baseline": "b"}],
                "static": [{"condition": "python-3.15", "status": "RISK", "where": hostile, "cause": hostile}]}
            F.save_result(d, {"id": "b", "phases": OKP})
            F.save_result(d, {"id": "p", "phases": OKP[:2] + [{"phase": "test", "rc": 1, "outcome": "failure",
                              "junit": {"total": 3, "failed": [{"id": "t::" + hostile, "message": hostile * 20}]}}]})
            rep = F.report(plan, d, CONDS, F.dt.date(2026, 10, 14))
            rep["tool"] = "o/r@" + hostile
            md = F.markdown(rep)
        lines = md.splitlines()
        self.assertEqual([l for l in lines if l.startswith("#")],
                         ["## What will break, and when", f"### {CONDS['python-3.15']['title']} — BREAK"])
        self.assertNotIn("\x1b", md)
        for l in lines:
            if l.startswith("| "):
                self.assertEqual(len(re.findall(r"(?<!\\)\|", l)), 5, l)  # 4 cells
            self.assertLessEqual(len(l), 700, l)
            outside_code = re.sub(r"`[^`]*`", "", l)
            self.assertNotRegex(outside_code, r"(?<!\\)<img|(?<!\\)\[click(?<!\\)\]", l)
            if "`" in l:
                self.assertEqual(l.count("`") % 2, 0, l)


class ExecTests(unittest.TestCase):
    def test_timeout_and_skip(self):
        with tempfile.TemporaryDirectory() as d:
            F.exec_phase(d, "r", "install", "sleep 5", None, CONDS, timeout=1)
            F.exec_phase(d, "r", "test", "true", None, CONDS, timeout=10)
            res = F.load_result(d, "r")
            self.assertEqual([(p["phase"], p["outcome"]) for p in res["phases"]], [("install", "timeout"), ("test", "skipped")])

    def test_junit_collected(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = ('python3 -c "import os,sys; p=[a for a in os.environ[\'PYTEST_ADDOPTS\'].split() if a.startswith(\'--junitxml=\')][0][11:]; '
                   'open(p,\'w\').write(\'<testsuite><testcase classname=\\"m\\" name=\\"t1\\"/><testcase classname=\\"m\\" name=\\"t2\\">'
                   '<failure message=\\"bad\\"/></testcase></testsuite>\'); sys.exit(1)"')
            F.exec_phase(d, "r", "test", cmd, None, CONDS, timeout=30)
            ph = F.load_result(d, "r")["phases"][-1]
            self.assertEqual(ph["junit"], {"total": 2, "failed": [{"id": "m::t2", "message": "bad"}]})

    def test_local_cmd_workdir(self):
        with tempfile.TemporaryDirectory() as d:
            r = {"id": "u2604-python-oldest", "runsOn": "ubuntu-26.04", "lang": "python", "version": "3.9", "condition": "ubuntu-26.04"}
            a = F.argparse.Namespace(install="i", test="t", apt="", env=[], timeout=60, node="/n", actions="/a", tzdir="/z",
                                     repo=d, out=os.path.join(d, "res", "repoA"), cpus=2, memory="4g", workdir=None)
            self.assertNotIn("/home/runner/.cache", " ".join(F.local_cmd(r, a)))
            a.workdir = os.path.join(d, "w")
            cmd = " ".join(F.local_cmd(r, a))
            run_dir = os.path.join(d, "w", "runs", "repoA", "u2604-python-oldest")
            for sub, target in (("work", "/home/runner/work"), ("tmp", "/tmp"), ("toolcache", "/opt/hostedtoolcache")):
                self.assertIn(f"{run_dir}/{sub}:{target}", cmd)
            self.assertIn(f"{d}/w/cache/26.04:/home/runner/.cache", cmd)
            self.assertEqual(os.stat(os.path.join(run_dir, "tmp")).st_mode & 0o7777, 0o1777)
            self.assertIn("--ulimit nofile=65536:65536 --ulimit stack=16777216:-1", cmd)

    def test_condition_env(self):
        self.assertEqual(F.condition_env("canada-tz-2026-11-01:zone-only", CONDS), {"TZ": "America/Edmonton"})
        self.assertEqual(F.condition_env("ubuntu-26.04", CONDS), {})
        self.assertEqual(F.condition_env(None, CONDS), {})



class CauseLineTests(unittest.TestCase):
    """Report cause lines name the failing tests, never a log line whose counts are all zero."""

    def test_failed_tests_per_runner(self):
        log = ["ERROR: test_x (pkg.tests.T.test_x)", "FAIL [0.011s]: test_y (pkg.tests.T.test_y)",
               "FAILED tests/test_a.py::test_b - AssertionError: 1 != 2",
               "TestCertExpiration#test_certs_not_expired [test/test_cert.rb:46]:",
               "rspec ./spec/foo_spec.rb:12 # Foo works", "--- FAIL: TestParse (0.00s)",
               "test parser::tests::bad ... FAILED", "not ok 3 - parses dates", "  ● Clock › keeps time",
               "tests/test_a.py::test_c PASSED", "ERROR: test_x (pkg.tests.T.test_x)"]
        got = F.failed_tests(log)
        self.assertEqual(len(got), 9)  # every format once; passing line and duplicate dropped
        self.assertNotIn("tests/test_a.py::test_c PASSED", got)

    def test_zero_count_lines_are_not_causes(self):
        log = ["    10 successes, 0 resets, 30 refused, 0 failures, 0 read timeouts",
               "    5 restarts, 711 attempts, 0 resets, 0 refused, 516 success after restart, 0 write error",
               "950 runs, 2832 assertions, 1 failures, 0 errors, 7 skips", "OSError: [Errno 24] Too many open files"]
        self.assertEqual(F.error_lines(log), log[2:])

    def test_exec_records_named_failures(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = "printf 'Run options: --seed 1\\n  1) Failure:\\nTestCert#test_not_expired [test/t.rb:46]:\\n" \
                  "9 runs, 9 assertions, 1 failures, 0 errors, 0 skips\\n'; exit 1"
            F.exec_phase(d, "r", "test", cmd, None, CONDS, timeout=10)
            ph = F.load_result(d, "r")["phases"][-1]
        self.assertEqual(ph["failed"], ["TestCert#test_not_expired [test/t.rb:46]:"])
        self.assertEqual(F.cause_lines(ph), ph["failed"])

    def test_cause_prefers_named_failures(self):
        ph = {"failed": ["TestCert#test_not_expired [test/t.rb:46]:"], "errors": ["950 runs, 1 failures"],
              "detail": ""}
        self.assertEqual(F.cause_lines(ph), ph["failed"])
        self.assertEqual(F.cause_lines({"errors": ["a error", "b error"], "tail": []}), ["a error", "b error"])


class FaketimeTests(unittest.TestCase):
    COND = "canada-tz-2026-11-01"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.tmp.name, "cache")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def fake_lib(self):
        lib = F.faketime_lib()
        os.makedirs(os.path.dirname(lib))
        open(lib, "w").close()
        return lib

    def tarball(self, makefile):
        """A source tarball shaped like libfaketime's, pinned in place of the real one."""
        root = os.path.join(self.tmp.name, "pkg")
        write(root, "libfaketime-x/src/Makefile", makefile)
        tgz = os.path.join(self.tmp.name, "work", f"libfaketime-{F.LIBFAKETIME['version']}.tar.gz")
        os.makedirs(os.path.dirname(tgz))
        subprocess.run(["tar", "-czf", tgz, "-C", root, "libfaketime-x"], check=True)
        sha = hashlib.sha256(open(tgz, "rb").read()).hexdigest()
        return os.path.dirname(tgz), unittest.mock.patch.dict(F.LIBFAKETIME, {"sha256": sha})

    def test_env_preloads_only_the_pinned_build(self):
        with self.assertRaises(SystemExit):  # no silent fallback to the distro build
            F.condition_env(self.COND, CONDS)
        lib = self.fake_lib()
        env = F.condition_env(self.COND, CONDS)
        self.assertEqual(env["LD_PRELOAD"], lib)
        self.assertTrue(lib.startswith(os.environ["XDG_CACHE_HOME"]))
        self.assertEqual(env["FAKETIME_DONT_FAKE_MONOTONIC"], "1")  # faking it hangs lock/queue timeouts

    def test_test_phase_skips_after_tz_failure(self):
        out = os.path.join(self.tmp.name, "out")
        F.record(out, "tz-python", "setup", "success")
        F.record(out, "tz-python", "tz", "failure", "clock probe failed")
        # no libfaketime build exists: the skip must come before the condition's environment is built
        self.assertEqual(F.exec_phase(out, "tz-python", "test", "true", self.COND, CONDS, 10), 0)
        res = F.load_result(out, "tz-python")
        self.assertEqual(res["phases"][-1]["outcome"], "skipped")
        self.assertEqual(F.verdict(res, run(OKP))[:2], ("UNKNOWN", "the tz condition could not be applied"))

    def test_tool_cache_wins_over_caller_xdg(self):
        with unittest.mock.patch.dict(os.environ, {"FUTURECI_CACHE": os.path.join(self.tmp.name, "tool")}):
            lib = self.fake_lib()  # built by the tz step, before the caller's env applies
            os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp.name, "caller")  # set by the caller's env
            self.assertEqual(F.condition_env(self.COND, CONDS)["LD_PRELOAD"], lib)

    def test_fetch_refuses_wrong_hash(self):
        dest = os.path.join(self.tmp.name, "x.tar.gz")
        with open(dest, "w") as f:
            f.write("not the pinned tarball")
        with self.assertRaises(SystemExit) as cm:
            F.fetch("http://127.0.0.1:9/never-fetched", dest, F.LIBFAKETIME["sha256"])
        self.assertIn("sha256 mismatch", str(cm.exception))

    def test_install_builds_once_into_cache(self):
        work, pin = self.tarball("libfaketime.so.1:\n\techo built > $@\n")
        with pin:
            lib = F.faketime_install(work)
            self.assertEqual(open(lib).read(), "built\n")
            os.rename(os.path.join(work, f"libfaketime-{F.LIBFAKETIME['version']}.tar.gz"), os.path.join(work, "moved"))
            self.assertEqual(F.faketime_install(work), lib)  # cached: no fetch, no rebuild

    def test_install_builds_the_posix_semaphore_variant(self):
        # the default flock semaphore makes stat() exit 1 once a process closes its fds
        work, pin = self.tarball("libfaketime.so.1:\n\techo '$(FAKETIME_COMPILE_CFLAGS)' > $@\n")
        with pin:
            lib = F.faketime_install(work)
        self.assertIn("-DFT_SEMAPHORE_BACKEND=FT_POSIX", open(lib).read())
        self.assertIn("-Wno-error", open(lib).read())

    def test_cache_path_names_the_build_variant(self):
        # an earlier flock build in the same version's cache dir must not be reused
        self.assertEqual(os.path.basename(os.path.dirname(F.faketime_lib())),
                         f"libfaketime-{F.LIBFAKETIME['version']}-posixsem")

    def test_install_build_failure_is_fatal(self):
        work, pin = self.tarball("libfaketime.so.1:\n\techo compiler says no; exit 2\n")
        with pin, self.assertRaises(SystemExit) as cm:
            F.faketime_install(work)
        self.assertIn("compiler says no", str(cm.exception))
        self.assertFalse(os.path.exists(F.faketime_lib()))

    def probe(self, script, timeout=30):
        with unittest.mock.patch.object(F, "FAKETIME_PROBE", script):
            F.faketime_probe(self.COND, CONDS, timeout=timeout)

    def test_probe_fails_closed(self):
        self.fake_lib()
        clock = CONDS[self.COND]["clock"]
        self.probe(f"print({clock!r})")
        cases = [("raise OSError(22, 'Invalid argument')", 30, "Invalid argument"),
                 ("import time; time.sleep(30)", 1, "hung"),
                 ("print('2026-10-14 23:00:00')", 30, "expected"),
                 ("print('garbage')", 30, "expected")]
        for script, timeout, msg in cases:
            with self.subTest(script=script), self.assertRaises(SystemExit) as cm:
                self.probe(script, timeout)
            self.assertIn(msg, str(cm.exception))


    def test_tz_command_probes_after_install(self):
        calls = []
        with unittest.mock.patch.object(F, "tz_install", lambda *a: calls.append("tz")), \
             unittest.mock.patch.object(F, "faketime_install", lambda *a: calls.append("build")), \
             unittest.mock.patch.object(F, "faketime_probe", lambda *a: calls.append("probe")):
            F.main(["tz", "--workdir", self.tmp.name])
        self.assertEqual(calls, ["tz", "build", "probe"])


def whl(name, py, abi, plat):
    return {"filename": f"{name}-1.0-{py}-{abi}-{plat}.whl"}


def sd(version, requires_python=None, yanked=False):
    return {"filename": f"x-{version}.tar.gz", "requires_python": requires_python, "yanked": yanked}


def pypi_doc(version, files, requires_python=None, releases=None):
    return {"info": {"version": version, "requires_python": requires_python}, "urls": files,
            "releases": releases if releases is not None else {version: files}}


class PyPIRecheckTests(unittest.TestCase):
    """Snapshot findings for Python 3.15 are re-read from PyPI; only flagged public names are looked up."""
    CAPPED = {"capd": {"project": "capd", "latest": "2.0", "latest_requires_python": "<3.15,>=3.10",
                       "fallback_on_3.15": "1.0"}}
    WHEELS = {"nowhl": {"project": "nowhl", "version": "3.0", "class": "older_only"},
              "srconly": {"project": "srconly", "version": "1.0", "class": "sdist_only"},
              "fine": {"project": "fine", "version": "1.0", "class": "pure"}}
    DEPS = {"capd": "pyproject.toml", "nowhl": "requirements.txt", "srconly": "requirements.txt",
            "fine": "requirements.txt", "private-internal-pkg": "requirements.txt"}

    def setUp(self):
        env = unittest.mock.patch.dict(os.environ, {"FUTURECI_PYPI": ""})
        env.start()
        self.addCleanup(env.stop)

    def findings(self, docs):
        asked = []

        def fetch(name):
            asked.append(name)
            d = docs[name]
            if isinstance(d, Exception):
                raise d
            return d
        out = F.py315_dep_findings(self.DEPS, self.CAPPED, self.WHEELS, fetch)
        return {f["cause"].split(" ")[0]: f for f in out}, sorted(asked)

    def test_wheel_class(self):
        lin = "manylinux_2_17_x86_64.manylinux2014_x86_64"
        self.assertEqual(F.wheel_class([]), "no_files")
        self.assertEqual(F.wheel_class([{"filename": "x-1.0.tar.gz"}]), "sdist_only")
        self.assertEqual(F.wheel_class([whl("x", "py3", "none", "any")]), "pure")
        self.assertEqual(F.wheel_class([whl("x", "py2.py3", "none", "any")]), "pure")
        self.assertEqual(F.wheel_class([whl("x", "cp315", "cp315", lin)]), "cp315")
        self.assertEqual(F.wheel_class([whl("x", "cp310", "abi3", lin)]), "abi3")
        self.assertEqual(F.wheel_class([whl("x", "cp313", "cp313", lin), whl("x", "cp314", "cp314", lin)]), "older_only")
        self.assertEqual(F.wheel_class([whl("x", "cp315", "cp315", "macosx_11_0_arm64"),
                                        whl("x", "cp315", "cp315", "win_amd64")]), "no_linux_x86_wheel")
        self.assertEqual(F.wheel_class([whl("x", "cp315", "cp315t", lin)]), "older_only")  # free-threaded only
        # pip on 3.15 takes py3X-none wheels for X <= 15 (semgrep 1.180.0, checked with pip download --python-version 3.15)
        self.assertEqual(F.wheel_class([whl("x", "cp310.cp314.py310.py314", "none", "manylinux_2_34_x86_64")]), "pure")
        self.assertEqual(F.wheel_class([whl("x", "py2.py3", "none", lin)]), "pure")
        self.assertEqual(F.wheel_class([whl("x", "py316", "none", lin)]), "older_only")
        self.assertEqual(F.wheel_class([whl("x", "py316", "none", "any")]), "no_linux_x86_wheel")
        self.assertEqual(F.wheel_class([whl("x", "cp315", "none", "manylinux1_x86_64")]), "cp315")
        self.assertEqual(F.wheel_class([whl("x", "cp312", "abi3", "linux_x86_64")]), "abi3")

    def test_wheel_class_needs_an_installable_unyanked_wheel(self):
        # Ubuntu 24.04 CPython 3.15 (glibc 2.39): musllinux, a newer interpreter's abi3, a yanked file,
        # a file whose own requires-python excludes 3.15 or a newer glibc does not install
        lin = "manylinux_2_17_x86_64"
        cp315 = dict(whl("x", "cp315", "cp315", lin))
        for files in ([whl("x", "cp315", "cp315", "musllinux_1_2_x86_64")],
                      [whl("x", "cp316", "abi3", lin)],
                      [dict(cp315, yanked=True), whl("x", "cp314", "cp314", lin)],
                      [dict(cp315, requires_python="<3.15"), whl("x", "cp314", "cp314", lin)],
                      [whl("x", "cp315", "cp315", "manylinux_2_40_x86_64")],
                      [whl("x", "cp315", "abi3", "any")],
                      [whl("x", "cp314", "none", lin)]):
            with self.subTest(files=files):
                self.assertIn(F.wheel_class(files), F.PY315_RISK_CLASSES)
        self.assertEqual(F.wheel_class([cp315, whl("x", "cp315", "cp315", "musllinux_1_2_x86_64")]), "cp315")
        for rp in ("<3.15rc1", ">=3.15.0.1"):
            self.assertEqual(F.wheel_class([dict(cp315, requires_python=rp)]), "no_files", rp)
        self.assertEqual(F.wheel_class([dict(cp315, requires_python="<3.15.0.1")]), "cp315")
        self.assertEqual(F.wheel_class([dict(cp315, requires_python=">=3.5.*")]), "undecided")
        self.assertEqual(F.wheel_class([dict(cp315, requires_python=">=3.5.*"), cp315]), "cp315")
        self.assertEqual(F.wheel_class([dict(cp315, yanked=True), sd("1.0")]), "sdist_only")
        self.assertEqual(F.wheel_class([dict(whl("x", "py3", "none", "any"), yanked=True)]), "no_files")

    def test_uninstallable_wheels_keep_the_finding(self):
        lin = "manylinux_2_17_x86_64"
        for files in ([whl("nowhl", "cp315", "cp315", "musllinux_1_2_x86_64"), sd("3.1")],
                      [whl("nowhl", "cp316", "abi3", lin), sd("3.1")],
                      [dict(whl("nowhl", "cp315", "cp315", lin), yanked=True), whl("nowhl", "cp314", "cp314", lin), sd("3.1")]):
            with self.subTest(files=files):
                got, _ = self.findings({"capd": pypi_doc("2.1", [whl("capd", "py3", "none", "any")]),
                                        "nowhl": pypi_doc("3.1", files),
                                        "srconly": pypi_doc("1.1", [whl("srconly", "py3", "none", "any")])})
                self.assertEqual(list(got), ["nowhl"])
                self.assertEqual(got["nowhl"]["cause"], "nowhl 3.1 has no cp315 wheel: builds from source on 3.15")

    def test_per_file_python_exclusions_keep_the_finding(self):
        lin = "manylinux_2_17_x86_64"
        for rp in ("<3.15rc1", ">=3.15.0.1"):
            with self.subTest(rp=rp):
                got, _ = self.findings({"capd": pypi_doc("2.1", [whl("capd", "py3", "none", "any")]),
                                        "nowhl": pypi_doc("3.1", [dict(whl("nowhl", "cp315", "cp315", lin), requires_python=rp)]),
                                        "srconly": pypi_doc("1.1", [whl("srconly", "py3", "none", "any")])})
                self.assertEqual(list(got), ["nowhl"])
        got, _ = self.findings({"capd": pypi_doc("2.1", [whl("capd", "py3", "none", "any")]),
                                "nowhl": pypi_doc("3.1", [dict(whl("nowhl", "cp315", "cp315", lin), requires_python="<3.15.0.1")]),
                                "srconly": pypi_doc("1.1", [whl("srconly", "py3", "none", "any")])})
        self.assertEqual(got, {})

    def test_undecidable_requires_python_keeps_the_snapshot_finding(self):
        lin = "manylinux_2_17_x86_64"
        got, _ = self.findings({"capd": pypi_doc("2.1", [whl("capd", "py3", "none", "any")], ">=3.5.*"),
                                "nowhl": pypi_doc("3.1", [dict(whl("nowhl", "cp315", "cp315", lin), requires_python=">=3.5.*")]),
                                "srconly": pypi_doc("1.1", [dict(sd("1.1"), requires_python="~=3.15rc1")])})
        self.assertEqual(sorted(got), ["capd", "nowhl", "srconly"])
        for f in got.values():
            self.assertTrue(f["cause"].endswith("(PyPI's requires-python not decidable; snapshot of 2026-10-11)"), f["cause"])
        self.assertTrue(got["capd"]["cause"].startswith("capd 2.0 requires-python <3.15,>=3.10: pip falls back to 1.0"))
        # a capped latest release whose fallback rests on an undecidable file says so instead of naming one
        rel = {"2.1": [sd("2.1", "<3.15")], "2.0": [sd("2.0", ">=3.5.*")], "1.0": [sd("1.0")]}
        got, _ = self.findings({"capd": pypi_doc("2.1", rel["2.1"], "<3.15", releases=rel),
                                "nowhl": pypi_doc("3.1", [whl("nowhl", "cp315", "cp315", lin)]),
                                "srconly": pypi_doc("1.1", [whl("srconly", "py3", "none", "any")])})
        self.assertEqual(got["capd"]["cause"], "capd 2.1 requires-python <3.15: pip's fallback on 3.15 is not "
                                               "decidable (a requires-python on PyPI cannot be read)")

    def test_no_wheel_and_no_source_names_pips_fallback(self):
        rel = {"3.0": [whl("nowhl", "cp315", "cp315", "manylinux_2_28_x86_64")], "3.1": [whl("nowhl", "cp315", "cp315", "win_amd64")]}
        got, _ = self.findings({"capd": pypi_doc("2.1", [whl("capd", "py3", "none", "any")]),
                                "nowhl": pypi_doc("3.1", rel["3.1"], releases=rel),
                                "srconly": pypi_doc("1.1", [whl("srconly", "py3", "none", "any")])})
        self.assertEqual(got["nowhl"]["cause"], "nowhl 3.1 has no cp315 wheel and no source release: pip falls back to 3.0 on 3.15")

    def test_snapshot_has_no_py3_platform_wheel_as_older_only(self):
        # py2.py3-none-<linux> wheels install on any CPython 3; the 2026-10-11 scan split no tags and called them older_only
        rows = F.load_tsv("top3000-wheels.tsv")
        bad = [n for n, r in rows.items() if r["class"] == "older_only"
               and any(F.py3_tag_ok(t) for tag in r["detail"].split() for t in tag.split("."))]
        self.assertEqual(bad, [])
        self.assertEqual((rows["tbb"]["class"], rows["semgrep"]["class"]), ("pure", "pure"))
        # and it took musllinux, PyPy and py2 wheels for installable ones (re-read 2026-10-16 at the snapshot versions)
        self.assertEqual([rows[n]["class"] for n in ("lupa", "lazy-object-proxy", "langdetect", "pure-sasl")],
                         ["older_only", "older_only", "no_linux_x86_wheel", "no_linux_x86_wheel"])

    def test_fallback_skips_yanked_prerelease_and_capped(self):
        rel = {"1.0": [sd("1.0", ">=3.8")], "1.5": [sd("1.5", ">=3.8", yanked=True)],
               "1.9rc1": [sd("1.9rc1", ">=3.8")], "1.2": [sd("1.2")],
               "1.10": [sd("1.10", ">=3.9, !=3.15.*")], "2.0": [sd("2.0", "<3.15")], "2.1": []}
        self.assertEqual(F.fallback_315(rel), "1.2")
        self.assertIsNone(F.fallback_315({"2.0": [sd("2.0", "<3.15")]}))

    def test_fallback_follows_pep440_order_and_per_file_eligibility(self):
        lin = "manylinux_2_17_x86_64"
        self.assertEqual(F.fallback_315({"1.0.post2": [sd("1.0.post2")], "1.0.1": [sd("1.0.1")]}), "1.0.1")
        self.assertEqual(F.fallback_315({"1.0.post2": [sd("1.0.post2")], "1.0": [sd("1.0")]}), "1.0.post2")
        self.assertEqual(F.fallback_315({"1!0.1": [sd("1!0.1")], "2.0": [sd("2.0")]}), "1!0.1")
        # the first file's cap is not the release's: an eligible sdist after a capped wheel counts
        self.assertEqual(F.fallback_315({"1.0": [sd("1.0")],
                                         "1.5": [dict(whl("x", "cp314", "cp314", lin), requires_python="<3.15"), sd("1.5")]}), "1.5")
        self.assertEqual(F.fallback_315({"1.0": [sd("1.0")], "1.5": [sd("1.5", "<3.15"), sd("1.5", ">=3.8")]}), "1.5")
        # a release with only a Windows wheel or a musllinux wheel installs nothing here
        self.assertEqual(F.fallback_315({"1.0": [sd("1.0")], "1.5": [whl("x", "cp315", "cp315", "win_amd64")],
                                         "1.6": [whl("x", "cp315", "cp315", "musllinux_1_2_x86_64")]}), "1.0")
        self.assertEqual(F.fallback_315({"1.0": [sd("1.0")], "1.5": [whl("x", "py3", "none", "any")]}), "1.5")
        # pip takes a pre-release only when no final release qualifies (yanked finals count for that) and,
        # unpinned, never a yanked file (checked with pip 26.2.1 install --dry-run on offline indexes)
        self.assertEqual(F.fallback_315({"2.0b1": [sd("2.0b1")], "2.0a3": [sd("2.0a3")], "1.9": [sd("1.9", "<3.15")]}), "2.0b1")
        self.assertIsNone(F.fallback_315({"1.0": [sd("1.0", yanked=True)], "0.9": [sd("0.9", yanked=True)]}))
        self.assertIsNone(F.fallback_315({"1.0": [dict(whl("x", "cp315", "cp315", lin), yanked=True)]}))
        self.assertEqual(F.fallback_315({"1.0": [dict(whl("x", "cp315", "cp315", lin), yanked=True)], "0.9": [sd("0.9")]}), "0.9")
        self.assertEqual(F.fallback_315({"1.0": [sd("1.0", yanked=True), sd("1.0.zip")]}), "1.0")
        self.assertIsNone(F.fallback_315({"1.0": [sd("1.0", yanked=True)], "2.0b1": [sd("2.0b1")]}))
        # per-file requires-python with full PEP 440 versions
        self.assertEqual(F.fallback_315({"1.0": [sd("1.0", "<3.15rc1")], "0.9": [sd("0.9")]}), "0.9")
        self.assertEqual(F.fallback_315({"1.0": [sd("1.0", ">=3.15.0.1")], "0.9": [sd("0.9")]}), "0.9")
        self.assertEqual(F.fallback_315({"1.0": [sd("1.0", "<3.15.0.1")], "0.9": [sd("0.9")]}), "1.0")
        # an undecidable requires-python makes the answer undecided only where it could change it
        self.assertEqual(F.fallback_315({"1.0": [sd("1.0", ">=3.5.*")], "0.9": [sd("0.9")]}), F.UNDECIDED)
        self.assertEqual(F.fallback_315({"2.0": [sd("2.0")], "1.0": [sd("1.0", ">=3.5.*")]}), "2.0")
        self.assertEqual(F.fallback_315({"2.0": [sd("2.0", ">=3.5.*", yanked=True)], "1.0": [sd("1.0")]}), "1.0")
        self.assertEqual(F.fallback_315({"2.0": [sd("2.0", ">=3.5.*", yanked=True)], "3.0b1": [sd("3.0b1")]}), F.UNDECIDED)
        self.assertEqual(F.fallback_315({"3.0b1": [sd("3.0b1", ">=3.5.*")], "1.0": [sd("1.0")]}), "1.0")
        self.assertEqual(F.fallback_315({"1.0": [sd("1.0")], "not a version": [sd("9")]}), "1.0")

    def test_pep440_key_orders_like_the_spec(self):
        order = ["1.0.dev456", "1.0a1", "1.0a2.dev456", "1.0a12.dev456", "1.0a12", "1.0b1.dev456", "1.0b2",
                 "1.0b2.post345.dev456", "1.0b2.post345", "1.0rc1.dev456", "1.0rc1", "1.0", "1.0.post456.dev34",
                 "1.0.post456", "1.0.1", "1.0.15", "1.1.dev1", "1!0.1"]
        self.assertEqual(sorted(reversed(order), key=lambda v: F.pep440_key(v)[1]), order)
        self.assertEqual(F.pep440_key("1.0")[1], F.pep440_key("1.0.0")[1])
        self.assertEqual(F.pep440_key("1.0-1")[1], F.pep440_key("1.0.post1")[1])
        self.assertEqual(F.pep440_key("1.0C1")[1], F.pep440_key("1.0rc1")[1])
        self.assertEqual([F.pep440_key(v)[0] for v in ("1.0", "1.0.post1", "1.0rc1", "1.0.dev1")], [False, False, True, True])
        self.assertIsNone(F.pep440_key("1.0+local"))

    def test_fixed_since_snapshot_are_dropped(self):
        lin = "manylinux_2_28_x86_64"
        got, asked = self.findings({
            "capd": pypi_doc("2.1", [whl("capd", "py3", "none", "any")], "<3.16,>=3.10"),
            "nowhl": pypi_doc("3.1", [whl("nowhl", "cp315", "cp315", lin)]),
            "srconly": pypi_doc("1.1", [whl("srconly", "cp39", "abi3", lin)])})
        self.assertEqual(got, {})
        self.assertEqual(asked, ["capd", "nowhl", "srconly"])  # never "fine", never the private name

    def test_still_flagged_uses_pypi_today(self):
        lin = "manylinux_2_28_x86_64"
        rel = {"1.0": [sd("1.0", ">=3.8")], "1.1": [sd("1.1", ">=3.8")], "2.2": [sd("2.2", "<3.15,>=3.10")]}
        got, _ = self.findings({
            "capd": pypi_doc("2.2", [whl("capd", "py3", "none", "any")], "<3.15,>=3.10", rel),
            "nowhl": pypi_doc("3.2", [whl("nowhl", "cp314", "cp314", lin), sd("3.2")]),
            "srconly": pypi_doc("1.3", [{"filename": "srconly-1.3.tar.gz"}])})
        self.assertEqual(got["capd"]["cause"], "capd 2.2 requires-python <3.15,>=3.10: pip falls back to 1.1 on 3.15")
        self.assertEqual(got["capd"]["where"], "pyproject.toml")
        self.assertEqual(got["nowhl"]["cause"], "nowhl 3.2 has no cp315 wheel: builds from source on 3.15")
        self.assertEqual(got["srconly"]["cause"], "srconly 1.3 has no cp315 wheel: builds from source on 3.15")
        self.assertTrue(all(f["status"] == "RISK" and f["condition"] == "python-3.15" for f in got.values()))

    def test_unreadable_pypi_keeps_dated_snapshot(self):
        import urllib.error
        got, _ = self.findings({"capd": urllib.error.URLError("down"), "nowhl": {"unexpected": 1},
                                "srconly": ValueError("PyPI answer too large")})
        tail = f" (PyPI not read; snapshot of {F.PY315_SNAPSHOT})"
        self.assertEqual(got["capd"]["cause"], "capd 2.0 requires-python <3.15,>=3.10: pip falls back to 1.0 on 3.15" + tail)
        self.assertEqual(got["nowhl"]["cause"], "nowhl 3.0 has no cp315 wheel: builds from source on 3.15" + tail)
        self.assertEqual(got["srconly"]["cause"], "srconly 1.0 has no cp315 wheel: builds from source on 3.15" + tail)

    def test_offline_and_cap_never_fetch(self):
        def boom(name):
            raise AssertionError(f"fetched {name}")
        with unittest.mock.patch.dict(os.environ, {"FUTURECI_PYPI": "off"}):
            out = F.py315_dep_findings(self.DEPS, self.CAPPED, self.WHEELS, boom)
        self.assertEqual(len(out), 3)
        with unittest.mock.patch.object(F, "PYPI_MAX", 1):
            res = F.recheck_pypi(["a", "b", "c"], lambda n: pypi_doc("1", [whl(n, "py3", "none", "any")]))
        self.assertIsNotNone(res["a"])
        self.assertEqual((res["b"], res["c"]), (None, None))

    def test_pypi_json_refuses_oversize_answer(self):
        class Resp:
            def __init__(self, body):
                self.body = body
            def read(self, n):
                return self.body[:n]
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        seen = []
        def fake(req, timeout):
            seen.append((req.full_url, timeout))
            return Resp(b'{"info": {"version": "1"}}' + b" " * 64)
        with unittest.mock.patch.object(F.urllib.request, "urlopen", fake), unittest.mock.patch.object(F, "PYPI_BYTES", 32):
            with self.assertRaises(ValueError):
                F.pypi_json("zope-interface")
        self.assertEqual(seen, [("https://pypi.org/pypi/zope-interface/json", F.PYPI_TIMEOUT)])

    def test_hostile_pypi_text_renders_inert(self):
        got, _ = self.findings({
            "capd": pypi_doc("9<img src=x>|[a](b)", [whl("capd", "py3", "none", "any")], "<3.15"),
            "nowhl": pypi_doc("1", [whl("nowhl", "py3", "none", "any")]),
            "srconly": pypi_doc("1", [whl("srconly", "py3", "none", "any")])})
        rep = {"repo": "r", "rows": [{"condition": "python-3.15", "title": "t", "when": "w", "status": "RISK",
                                      "static": list(got.values()), "runs": [], "fix": "f", "source": "s"}]}
        md = F.markdown(rep)
        line = next(l for l in md.splitlines() if l.startswith("- RISK: capd"))
        self.assertIsNone(re.search(r"(?<!\\)[<>\[\]|]", line.split(" — ")[0]))  # every active character escaped
        self.assertIn("capd 9\\<img src=x\\>\\|\\[a\\](b)", line)


class WorkflowTests(unittest.TestCase):
    """The tool code is pinned by the caller: no default ref, resolved once, the same commit everywhere."""

    def test_tool_ref_required_and_resolved_once(self):
        import yaml
        with open(os.path.join(os.path.dirname(HERE), ".github", "workflows", "future-ci.yml")) as f:
            wf = yaml.safe_load(f)
        inp = wf[True]["workflow_call"]["inputs"]["tool-ref"]  # YAML 1.1 reads the key `on` as True
        self.assertTrue(inp.get("required"))
        self.assertNotIn("default", inp)
        refs = {}
        for name, job in wf["jobs"].items():
            for st in job["steps"]:
                if st.get("uses", "").startswith("actions/checkout@") and st.get("with", {}).get("path") == ".future-ci":
                    refs[name] = st["with"]["ref"]
        self.assertEqual(refs, {"plan": "${{ inputs.tool-ref }}", "run": "${{ needs.plan.outputs.tool-sha }}",
                                "report": "${{ needs.plan.outputs.tool-sha }}"})
        self.assertEqual(wf["jobs"]["plan"]["outputs"]["tool-sha"], "${{ steps.tool.outputs.sha }}")
        self.assertIn("plan", wf["jobs"]["report"]["needs"])


if __name__ == "__main__":
    unittest.main()
