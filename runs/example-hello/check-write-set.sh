#!/bin/bash
# Scope gate: fail if anything outside the declared write set changed.
# Copy this for your own runs and edit the two names in the case statement.
#
# bash, not sh, and that is load-bearing. `read -d` is a bash extension. Under
# the worker container's /bin/sh (dash) the loop dies on its first line with
# "read: Illegal option -d", the body never runs, and the gate prints
# "write set in scope" and exits 0 no matter what changed. A scope gate that
# cannot go red is worse than none: it reads green in the ledger while an
# executor writes anywhere it likes. Five separate rounds rediscovered this and
# wrote it into a constraints file; nobody changed the shebang.
#
# -z and the read loop are not decoration: iterating `$(git status --porcelain)`
# word-splits on whitespace, so one filename with a space in it became fragments
# that match nothing, and the gate was red forever with no way to pass.
# A rename is two records: the new name, then the source path on its own. Reading
# the source as a record strips three characters off a bare path and reports a
# mangled name, so `skip` drops it the way the engine's own parser does.
#
# pipefail, because `|| bad=1` below reads the exit of the `{ ... }` block and
# not git's. When `git status` itself fails the block gets empty input, the loop
# never runs, the block exits 0, and the gate prints "write set in scope" having
# looked at nothing. The engine runs a gate's command under `bash -o pipefail`,
# but that sets the option in the shell it starts, and a script run by its own
# shebang is a fresh bash with the defaults.
set -o pipefail
bad=0
git status --porcelain -z --untracked-files=all | {
  skip=0
  while IFS= read -r -d "" rec; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$rec" in R*|C*|?R*|?C*) skip=1 ;; esac
    f=${rec#???}
    case "$f" in
      cli.py|test_cli.py) ;;
      *) echo "OUT OF SCOPE: $f"; exit 1;;
    esac
  done
} || bad=1
[ $bad -eq 0 ] && echo "write set in scope"
exit $bad
