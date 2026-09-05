#!/bin/sh
# Scope gate (SPEC §5): fail if anything outside the declared write set changed.
# Copy this for your own runs and edit the two names in the case statement.
#
# -z and the read loop are not decoration: iterating `$(git status --porcelain)`
# word-splits on whitespace, so one filename with a space in it became fragments
# that match nothing, and the gate was red forever with no way to pass.
bad=0
git status --porcelain -z --untracked-files=all | while IFS= read -r -d "" rec; do
  f=${rec#???}
  case "$f" in
    cli.py|test_cli.py) ;;
    *) echo "OUT OF SCOPE: $f"; exit 1;;
  esac
done || bad=1
[ $bad -eq 0 ] && echo "write set in scope"
exit $bad
