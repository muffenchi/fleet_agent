#!/bin/sh
# Needs: CAPTAIN_SRC=<patched Captain tree>, and `npm i jsdom esbuild` somewhere (set UITEST_DIR to that folder).
cd "$(dirname "$0")/.." || exit 1
python3 tests/ui_rig.py > /tmp/ui_rig.out 2>/tmp/ui_rig.err &
RIG=$!
for i in $(seq 1 50); do [ -s /tmp/ui_rig.out ] && break; sleep 0.2; done
[ -s /tmp/ui_rig.out ] || { cat /tmp/ui_rig.err; kill $RIG 2>/dev/null; exit 1; }
cp tests/ui_smoke.mjs "${UITEST_DIR:-.}/ui_smoke.mjs"
(cd "${UITEST_DIR:-.}" && node ui_smoke.mjs "$(cat /tmp/ui_rig.out)" "$OLDPWD/fleet_agent/www"); STATUS=$?
kill $RIG 2>/dev/null; wait $RIG 2>/dev/null
exit $STATUS
