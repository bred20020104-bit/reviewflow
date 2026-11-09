"""Generate self-authored read-only review fixtures; never import/execute cases.

Run from the trusted tool checkout: python -I benchmarks/generate_corpus.py NEW_DIRECTORY
The destination must not exist. Regeneration produces a new corpus, never overwrites one.
"""

import hashlib
import json
import sys
from pathlib import Path

# (family, requirement, defective source, reference source, root cause)
FAMILIES = [
    ("index", "Return the last element of a nonempty sequence.",
     "def last(items):\n    return items[len(items)]\n",
     "def last(items):\n    return items[len(items) - 1]\n", "off-by-one"),
    ("average", "Return the arithmetic mean; return 0 for an empty sequence.",
     "def average(items):\n    return sum(items) / len(items)\n",
     "def average(items):\n    return sum(items) / len(items) if items else 0\n", "empty-division"),
    ("default", "Each call without a list must return an independent one-element list.",
     "def collect(value, items=[]):\n    items.append(value)\n    return items\n",
     "def collect(value, items=None):\n    items = [] if items is None else items\n    items.append(value)\n    return items\n",
     "shared-default"),
    ("sort", "Return a sorted copy without mutating the input list.",
     "def ordered(items):\n    return items.sort()\n",
     "def ordered(items):\n    return sorted(items)\n", "inplace-return"),
    ("boolean", "Parse exactly 'true' as True and 'false' as False; reject other text.",
     "def parse_bool(text):\n    return bool(text)\n",
     "def parse_bool(text):\n    if text not in ('true', 'false'):\n        raise ValueError('invalid boolean')\n    return text == 'true'\n",
     "truthy-string"),
    ("discount", "A 20 percent discount on price 100 must return 80. Percent is in [0,100].",
     "def discount(price, percent):\n    return price * (1 - percent)\n",
     "def discount(price, percent):\n    return price * (1 - percent / 100)\n", "percent-units"),
    ("auth", "Grant access only when both the active and admin flags are True.",
     "def allowed(active, admin):\n    return active or admin\n",
     "def allowed(active, admin):\n    return active and admin\n", "authorization-or"),
    ("sql", "Look up a name using a DB-API SQLite connection; names are arbitrary user input.",
     "def lookup(connection, name):\n    return connection.execute(\"SELECT id FROM users WHERE name = '\" + name + \"'\").fetchall()\n",
     "def lookup(connection, name):\n    return connection.execute('SELECT id FROM users WHERE name = ?', (name,)).fetchall()\n",
     "sql-concatenation"),
    ("html", "Render arbitrary user text as text inside a div, never as HTML markup.",
     "def render(text):\n    return '<div>' + text + '</div>'\n",
     "import html\n\ndef render(text):\n    return '<div>' + html.escape(text) + '</div>'\n", "html-injection"),
    ("dedup", "Remove duplicates while preserving the first occurrence order of hashable items.",
     "def unique(items):\n    return list(set(items))\n",
     "def unique(items):\n    return list(dict.fromkeys(items))\n", "order-loss"),
    ("range", "Return every integer from start through end, including both endpoints.",
     "def inclusive(start, end):\n    return list(range(start, end))\n",
     "def inclusive(start, end):\n    return list(range(start, end + 1))\n", "exclusive-end"),
    ("zero", "Use default only for None; zero, False and empty strings must be preserved.",
     "def choose(value, default):\n    return value or default\n",
     "def choose(value, default):\n    return default if value is None else value\n", "falsey-default"),
    ("copy", "Return a new independent nested configuration so editing it never changes input.",
     "def clone(config):\n    return config.copy()\n",
     "import copy\n\ndef clone(config):\n    return copy.deepcopy(config)\n", "shallow-copy"),
    ("remove", "Return all nonzero numbers from a list without changing the input.",
     "def nonzero(items):\n    for item in items:\n        if item == 0:\n            items.remove(item)\n    return items\n",
     "def nonzero(items):\n    return [item for item in items if item != 0]\n", "mutating-iteration"),
    ("minimum", "Return the minimum of a nonempty list, including all-positive inputs.",
     "def minimum(items):\n    result = 0\n    for value in items:\n        result = min(result, value)\n    return result\n",
     "def minimum(items):\n    result = items[0]\n    for value in items:\n        result = min(result, value)\n    return result\n", "invalid-initial-bound"),
]


def generate(destination):
    destination.mkdir()
    cases, defects, hashes = [], [], {}
    for index, (family, requirement, bad, good, cause) in enumerate(FAMILIES):
        for variant, source in (("a", bad), ("b", good)):
            case_id = f"case-{index + 1:02d}-{variant}"
            folder = destination / "cases" / case_id
            folder.mkdir(parents=True)
            (folder / "app.py").write_text(source, encoding="utf-8")
            (folder / "README.md").write_text("# Requirement\n\n" + requirement + "\n", encoding="utf-8")
            cases.append({"case_id": case_id, "group": family,
                          "split": "dev" if index < 5 else "test",
                          "truth_ids": [cause] if variant == "a" else []})
            for name in ("app.py", "README.md"):
                path = folder / name
                hashes[path.relative_to(destination).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            if variant == "a":
                defects.append({"case_id": case_id, "truth_id": cause, "requirement": requirement,
                                "reference_source": good, "origin": "self-authored artificial fixture"})
    (destination / "manifest.json").write_text(json.dumps({"cases": cases}, indent=2) + "\n")
    (destination / "annotations.json").write_text(json.dumps({"defects": defects}, indent=2) + "\n")
    (destination / "frozen-hashes.json").write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -I generate_corpus.py NEW_DIRECTORY")
    generate(Path(sys.argv[1]))
