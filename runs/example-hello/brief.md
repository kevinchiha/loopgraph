# Feature: two flags for the example CLI

`install.sh` creates the target repo for this run at
`<your projects dir>/loopgraph-example`. It is a throwaway, so this run is safe to
let loose.

Write set (stay inside it; the scope gate enforces this mechanically):

- `cli.py`
- `test_cli.py`

Each item below is handled separately: its own rounds and its own audit. Leave
your work in the working tree; the engine commits it once the audit accepts.

## Work items

- Add a `--hello` flag to `cli.py` that prints exactly `hello from the example`,
  and a test in `test_cli.py` proving it, in the same style as the test already
  there.
- Add a `--goodbye` flag to `cli.py` that prints exactly `goodbye from the
  example`, and a test for it in the same style.

## Done when

Both gates pass for every item: `python -m pytest -x -q`, and the scope gate
showing nothing outside the write set changed.
