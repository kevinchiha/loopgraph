# Feature: a --hello flag

`install.sh` creates the target repo for this run at
`<your projects dir>/loopgraph-example`. It is a throwaway, so this run is safe to
let loose.

Add a `--hello` flag to `cli.py` that prints exactly `hello from the example`, and
a test in `test_cli.py` that proves it, written in the same style as the test
already there.

Write set (stay inside it; the scope gate enforces this mechanically):

- `cli.py`
- `test_cli.py`

Done when both gates pass: `python -m pytest -x -q`, and the scope gate showing
nothing outside the write set changed.
