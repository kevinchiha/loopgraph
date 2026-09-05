"""One reader for a .env file, shared by `lg` and `ui.py`.

`lg` has no `.py` extension, so `ui.py` cannot import it; before this the two
carried their own copies of the rules, and a copy drifts. The last drift had
`lg where` hand the skill a path with its quotes still on while the container
got the real one, which is horrible to track down from either end.
"""

from __future__ import annotations

import os


def read_env(path: str | os.PathLike) -> dict[str, str]:
    """The settings in one .env file, read the way compose reads the same file.

    An absent file is an empty dict: there is no .env before the first install.
    Nothing else is consulted — no os.environ, no defaults. What a missing key
    means is the caller's to decide.
    """
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            # Match what compose does with the same file. Quotes are stripped
            # before the comment cut is even considered, so a quoted value keeps
            # a `#` that belongs to it.
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            elif " #" in v:
                v = v.split(" #", 1)[0].strip()
            out[k] = v
    return out
