# Changelog

What changed for people using loopgraph, newest first. Write your notes under
`## Unreleased` in the same commit as the change; `./release.sh X.Y.Z` renames that
heading to the version and date and opens a fresh `## Unreleased` above it.

## Unreleased

- `lg version` says which release you have and whether origin has a newer one.
- `lg update` moves a clean `main` to the newest release, refreshes the host venv
  when the dependencies changed, and restarts the worker and dispatcher. It refuses
  while a run is open or Temporal is down.
- The dashboard header shows the installed release and points at `lg update` when a
  newer one exists.
- `release.sh` for maintainers: checks, changelog, version bump, tag, push.
