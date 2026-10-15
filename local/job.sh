#!/bin/bash
# One hosted-job emulation: fresh checkout copy, apt packages, the released setup-* action under
# node 24 with the runner's env files, then futureci exec for install and test.
set -u
OUT=/out FCI="python3 /fci/futureci.py"
mkdir -p ~/work ~/gh /tmp/runner && cp -a /src ~/work/repo && cd ~/work/repo
export GITHUB_OUTPUT=~/gh/out GITHUB_ENV=~/gh/env GITHUB_PATH=~/gh/path GITHUB_STATE=~/gh/state
touch $GITHUB_OUTPUT $GITHUB_ENV $GITHUB_PATH $GITHUB_STATE
export RUNNER_TOOL_CACHE=/opt/hostedtoolcache AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache RUNNER_TEMP=/tmp/runner \
  RUNNER_OS=Linux RUNNER_ARCH=X64 GITHUB_ACTIONS=true CI=true GITHUB_WORKSPACE=$PWD
META=$(printf '{"os":"%s","lang":"%s","version":"%s","condition":"%s"}' "$(. /etc/os-release; echo $VERSION_ID)" \
  "${FCI_LANG:-}" "${FCI_VERSION:-}" "$FCI_COND")
if [ -n "$FCI_APT" ]; then
  sudo apt-get update -qq >/dev/null 2>&1; sudo apt-get install -y -qq $FCI_APT > ~/apt.log 2>&1 \
    || { $FCI record --id "$FCI_ID" --phase apt --outcome failure --detail "$(tail -5 ~/apt.log)" --out $OUT --meta "$META"; exit 0; }
fi
/node "$FCI_SETUP" > ~/setup.log 2>&1; rc=$?
grep -q '^::error' ~/setup.log && rc=1
$FCI record --id "$FCI_ID" --phase setup --outcome $([ $rc = 0 ] && echo success || echo failure) \
  --detail "$(grep -E '^::error|Error' ~/setup.log | tail -3; tail -2 ~/setup.log)" --out $OUT --meta "$META"
cp ~/setup.log $OUT/$FCI_ID.setup.log
[ $rc = 0 ] || exit 0
while IFS= read -r p; do [ -n "$p" ] && PATH="$p:$PATH"; done < $GITHUB_PATH
export PATH
# GITHUB_ENV: plain NAME=value lines and NAME<<EOF heredoc blocks
python3 - > ~/env.sh <<'PY'
import os, shlex
lines = open(os.environ["GITHUB_ENV"]).read().split("\n"); i = 0
while i < len(lines):
    l = lines[i]; i += 1
    if "<<" in l and "=" not in l.split("<<")[0]:
        k, d = l.split("<<", 1); v = []
        while i < len(lines) and lines[i] != d: v.append(lines[i]); i += 1
        i += 1; print(f"export {k}={shlex.quote(chr(10).join(v))}")
    elif "=" in l:
        k, v = l.split("=", 1); print(f"export {k}={shlex.quote(v)}")
PY
. ~/env.sh
case "$FCI_COND" in canada-tz-*) [[ "$FCI_COND" == *:zone-only ]] || { mkdir -p ~/tz && cp /tzcache/*.tar.gz ~/tz/ && $FCI tz --workdir ~/tz > $OUT/$FCI_ID.tz.log 2>&1 \
  || { $FCI record --id "$FCI_ID" --phase tz --outcome failure --detail "$(tail -3 $OUT/$FCI_ID.tz.log)" --out $OUT; exit 0; }; } ;; esac
$FCI exec --id "$FCI_ID" --phase install --out $OUT --timeout "$FCI_TIMEOUT" -- "$FCI_INSTALL" > /dev/null
$FCI exec --id "$FCI_ID" --phase test --condition "$FCI_COND" --out $OUT --timeout "$FCI_TIMEOUT" -- "$FCI_TEST" > /dev/null
exit 0
