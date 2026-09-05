#!/bin/sh
# Scope gate (SPEC §5): fail if anything outside the declared write set changed.
# Copy this for your own runs and edit the two names in the case statement.
#
# -z and the read loop are not decoration: iterating `$(git status --porcelain)`
# word-splits on whitespace, so one filename with a space in it became fragments
# that match nothing, and the gate was red forever with no way to pass.
# A rename is two records: the new name, then the source path on its own. Reading
# the source as a record strips three characters off a bare path and reports a
# mangled name, so `skip` drops it the way the engine's own parser does.
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
