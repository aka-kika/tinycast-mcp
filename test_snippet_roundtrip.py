"""Snippet frontmatter round-trip tests.

Guards the property that made a real bug possible: writes must be idempotent.
A body that already carries frontmatter must not gain a second block, and a
file that already has one must heal on the next write rather than compound.

Run:  uv run --script test_snippet_roundtrip.py
(Or with the `mcp` package installed: python3 test_snippet_roundtrip.py)
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
from pathlib import Path

# server.py imports `mcp` at module load. Stub it so these tests run with no
# dependencies installed; real behaviour under a live server is unchanged.
if "mcp" not in sys.modules:
    for name in ("mcp", "mcp.server", "mcp.server.fastmcp"):
        sys.modules[name] = types.ModuleType(name)

    class _FakeFastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda f: f

    sys.modules["mcp.server.fastmcp"].FastMCP = _FakeFastMCP

_spec = importlib.util.spec_from_file_location(
    "tinycast_mcp_server", Path(__file__).with_name("server.py")
)
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)

serialize = srv.serialize_snippet
parse = srv.parse_snippet_file

_failures: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        _failures.append(label)


def delimiters(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() == "---")


def write_tmp(content: str, suffix: str = ".md") -> Path:
    path = Path(tempfile.mkdtemp()) / f"snippet{suffix}"
    path.write_text(content, encoding="utf-8")
    return path


print("1. clean body")
out = serialize("Notes", "# Hello\nworld\n", "!n", True, False)
check("exactly one frontmatter block", delimiters(out) == 2 and out.count("name:") == 1)
check("opens with a delimiter", out.startswith("---\n"))
check("body preserved", "# Hello" in out and "world" in out)

print("2. body that already carries frontmatter")
dirty = '---\nname: "Old"\nkeyword: "!old"\nenabled: true\n---\n# Body\n'
out2 = serialize("Notes", dirty, "!n", True, False)
check("no second block added", delimiters(out2) == 2, f"delims={delimiters(out2)}")
check("single name: key", out2.count("name:") == 1, f"name: x{out2.count('name:')}")
check("body preserved", "# Body" in out2)
check("stale frontmatter removed", '"Old"' not in out2 and '"!old"' not in out2)

print("3. writes are idempotent")
first = serialize("Notes", "# Body\n", "!n", True, False)
second = serialize("Notes", parse(write_tmp(first))["text"], "!n", True, False)
third = serialize("Notes", parse(write_tmp(second))["text"], "!n", True, False)
check("serialize/parse/serialize is stable", first == second == third)

print("4. a corrupted file heals instead of compounding")
corrupted = (
    '---\nname: "Meeting Notes"\nkeyword: "!meet"\nenabled: true\n'
    "show_confirmation: false\n---\n"
    '---\nname: "Meeting Notes"\nkeyword: "!meet"\nenabled: true\n'
    "show_confirmation: false\n---\n# Meeting\n\n{cursor}\n"
)
check("fixture really is doubled", corrupted.count("name:") == 2)
parsed = parse(write_tmp(corrupted))
healed = serialize("Meeting Notes", parsed["text"], parsed["keyword"],
                   parsed["enabled"], parsed["show_confirmation"])
check("healed to a single block", healed.count("name:") == 1, f"name: x{healed.count('name:')}")
check("keyword survived", 'keyword: "!meet"' in healed)
check("show_confirmation survived", "show_confirmation: false" in healed)
check("body survived", "# Meeting" in healed and "{cursor}" in healed)
reparsed = parse(write_tmp(healed))
check("healed file re-parses", reparsed["name"] == "Meeting Notes" and reparsed["keyword"] == "!meet")
check("healed file is now stable",
      healed == serialize("Meeting Notes", reparsed["text"], reparsed["keyword"],
                          reparsed["enabled"], reparsed["show_confirmation"]))

print()
if _failures:
    print(f"FAILED ({len(_failures)}): " + ", ".join(_failures))
    sys.exit(1)
print("ALL TESTS PASSED")
