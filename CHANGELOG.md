# Changelog

What changed for people using loopgraph, newest first. Write your notes under
`## Unreleased` in the same commit as the change; `./release.sh X.Y.Z` renames that
heading to the version and date and opens a fresh `## Unreleased` above it.

## Unreleased

- The dashboard says `waiting` on a run that is blocked on you, in the left rail and
  on the board, where it used to say `running`. `running` is also what it says while
  an executor works, which is most of a run's life, so the one run you had to answer
  looked like every other one.
- A run that has finished never says `waiting`, and its question, its options and its
  `lg approve` command come off the board with it. A run whose engine died keeps its
  last card in the record, and the page was still asking you to answer it.
- The browser tab says how many runs are waiting on you: `(2) loopgraph` with two of
  them, `loopgraph` with none. Its icon is a dot that turns blue at the same moment,
  so a dashboard left open in a background tab tells you a run needs you without you
  going and looking.
- You can tell one of the supervisor's reasons from the next. Each one starts at the
  left and its wrapped lines are indented under it, so eight reasons read as eight
  things and not as eleven lines running together — a reason that wrapped used to
  look exactly like a new reason starting.
- Sentences on the board stop at about 80 characters instead of running the full
  width of the window: the reasons, the rest of a round's fields, the question a run
  is asking and the line beside its status. The logs and the diff are untouched and
  still get the whole width.
- Round cards fold up. Each one is a box with an edge now, and a closed one is a
  single line — `item 2 · round 3 · accept · 4 files` — so a run of twelve rounds is
  twelve lines to read down instead of a page to scroll to the end of. The newest
  round is the one that opens; the rest you open and close yourself, and no poll
  ever changes what you left.
- The verdict word is coloured by what it means: green for `accept`, yellow while the
  supervisor is still out and on a `redo`, red on `escalated`. Both places the word
  appears take the same colour, so you can read a column of folded round cards and
  see which rounds went wrong without reading a word. A verdict the dashboard has
  never met keeps its plain colour and every letter of its text.
- Opening one of a round's two log panes gives it the room the closed one is not
  using — about 930px of a 1440px window instead of 516. Open both and they go back
  to sharing the row.
- A run has an address. Clicking one puts its id in the URL, so reloading the page
  comes back to the run you were reading instead of the top of the rail, and the URL
  is something you can bookmark or send to someone else. A hash naming a run that is
  not there, or one that is not a valid URL at all, opens the top run as before.
- Times in the rail read as `14m ago` and `3h ago` for the first day, and go back to
  the date after that. Hover one for the full timestamp, whichever way it is showing.
- The run you are reading is named at the top of the board, with its status and how
  long it has been going, and it stays there while you scroll. Three screens into a
  long run the only thing telling you which run it was is the highlight in the rail,
  off to the left where you are not looking. A run known only from its log files gets
  its name and nothing else, because there is no status or duration to give it.
- The board no longer says `no workflow for this run` about a run that plainly has
  one. That line was covering two different things: a directory of log files with
  nothing behind it, and a run Temporal knows perfectly well whose state just did not
  come back this time. The second now says `ledger did not answer this poll; the run
  is still there` and keeps its status, instead of denying the run named right above
  it.

## 0.2.0 - 2026-09-07

- Sweep runs: a `run.yaml` with detectors makes the engine remove what they report,
  one path group per item, until two passes in a row come back at or under the
  floor, or the deadline or the item cap lands first.
- Convergence items: once a run has added enough code, the engine inserts an item
  that only removes; its commit is refused if it adds more lines than it removes.
- `lg version` says which release you have and whether origin has a newer one.
- `lg update` moves a clean `main` to the newest release, refreshes the host venv
  when the dependencies changed, and restarts the worker and dispatcher. It refuses
  while a run is open or Temporal is down.
- The dashboard header shows the installed release and points at `lg update` when a
  newer one exists.
- `release.sh` for maintainers: checks, changelog, version bump, tag, push.
