#!/bin/sh
# Scope gate (SPEC §5): fail if anything outside the declared write set changed.
# Copy this for your own runs and edit the two filenames in the case statement.
changed=$(git status --porcelain | sed -e 's/^...//' -e 's/.* -> //')
bad=0
for f in $changed; do
  case "$f" in
    cli.py|test_cli.py) ;;
    *) echo "OUT OF SCOPE: $f"; bad=1;;
  esac
done
[ $bad -eq 0 ] && echo "write set in scope"
exit $bad
