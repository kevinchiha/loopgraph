#!/usr/bin/env bash
# Cut a release: check, date the changelog's Unreleased section, bump the
# version, commit, tag and push.
#
#   ./release.sh 0.2.0
#
# RELEASE_TESTS replaces the suite it runs before writing anything, which is how
# the tests for this script get past that step without running pytest inside a
# temporary clone.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Everything below is about the repository this script lives in, never the one
# the maintainer happens to be standing in.
cd "$ROOT"

die(){ printf 'refuse: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ checks ---
# All of them, and the suite last, before the first file is written: a release
# that stops half way leaves a checkout somebody has to unpick by hand.

VERSION="${1:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || die "version must look like X.Y.Z, got '$VERSION'"
TAG="v$VERSION"

BRANCH="$(git branch --show-current)"
[ -n "$BRANCH" ] || die "not on a branch (detached HEAD)."
[ "$BRANCH" = "main" ] || die "on branch $BRANCH, not main."

[ -z "$(git status --porcelain --untracked-files=no)" ] \
  || die "uncommitted changes. Commit or stash them first."

# --no-tags because git otherwise copies origin's tags in as a side effect of the
# fetch, which would turn "someone else already released this" into "you already
# have that tag" and hide which side of the release the collision is on.
git fetch --quiet --no-tags origin || die "could not reach origin."
[ "$(git rev-parse HEAD)" = "$(git rev-parse -q --verify origin/main || true)" ] \
  || die "main is not at origin/main. Pull or push first."

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  die "tag $TAG already exists locally."
fi
# Captured first, then tested: an ls-remote that failed prints nothing, and
# reading that silence as "no such tag on origin" would release on top of
# somebody else's tag the one time the network was the problem.
REMOTE_TAG="$(git ls-remote --tags origin "refs/tags/$TAG")" || die "could not reach origin."
if [ -n "$REMOTE_TAG" ]; then
  die "tag $TAG already exists on origin."
fi

# Whole lines, both of them. The file's intro names `## Unreleased` in code spans
# above the real heading, and a substring match would find those instead.
#
# Both reads are quiet about a file they could not open. With no CHANGELOG.md at
# all the refusal below is the answer, and grep's and awk's own "No such file"
# lines beside it only bury the sentence that says what to do.
SECTION="^## ${VERSION//./\\.} - "
has_notes(){ awk '/^## Unreleased$/{under=1; next} /^## /{under=0} under && NF {found=1}
                  END {exit !found}' CHANGELOG.md 2>/dev/null; }
if ! grep -qE "$SECTION" CHANGELOG.md 2>/dev/null && ! has_notes; then
  die "CHANGELOG.md has nothing under ## Unreleased and no ## $VERSION section."
fi

# bash -c so a multi-word RELEASE_TESTS is a command line and not an argv[0]
# nobody can execute.
bash -c "${RELEASE_TESTS:-.venv/bin/python -m pytest -q}" \
  || die "tests failed; nothing released."

# ------------------------------------------------------------------ writes ---

VERSION="$VERSION" TODAY="$(date +%Y-%m-%d)" python3 - <<'PY'
import os
import pathlib
import re
import sys

version, today = os.environ["VERSION"], os.environ["TODAY"]

# The line to bump is found before either file is written. A pyproject.toml with
# a second column-0 `version = "..."` — a [tool] table carrying one of its own —
# used to have every one of them rewritten, and by the time anyone read the file
# the changelog heading had already been renamed.
pyproject = pathlib.Path("pyproject.toml")
declared = pyproject.read_text(encoding="utf-8").splitlines(keepends=True)
at = [n for n, line in enumerate(declared)
      if re.fullmatch(r'version = ".*"', line.rstrip("\n"))]
if len(at) != 1:
    sys.exit('refuse: pyproject.toml must have exactly one version = "..." line, '
             f"found {len(at)}.")

# Only whole heading lines, the rule version.py reads the file by.
changelog = pathlib.Path("CHANGELOG.md")
lines = changelog.read_text(encoding="utf-8").splitlines(keepends=True)
dated = re.compile(rf"## {re.escape(version)} - ")
if not any(dated.match(line) for line in lines):
    out, renamed = [], False
    for line in lines:
        if not renamed and line.rstrip("\n") == "## Unreleased":
            # The heading becomes the release, and an empty one opens above it
            # for whatever gets written next.
            out.append(f"## Unreleased\n\n## {version} - {today}\n")
            renamed = True
        else:
            out.append(line)
    changelog.write_text("".join(out), encoding="utf-8")

declared[at[0]] = f'version = "{version}"\n'
pyproject.write_text("".join(declared), encoding="utf-8")
PY

git add CHANGELOG.md pyproject.toml
if git diff --cached --quiet; then
  # Both files were already at their release state: written by hand, or a run
  # that died between the commit and the tag. Committing nothing exits 1, which
  # under `set -e` would end the release here with no tag and no reason printed.
  echo "nothing to commit; tagging HEAD"
else
  git commit -m "Release $VERSION"
fi
git tag -a "$TAG" -m "$VERSION"
# Named refs, never --tags: that would push every stray tag on this machine.
git push origin main "$TAG"
echo "released $VERSION (tag $TAG pushed)"
