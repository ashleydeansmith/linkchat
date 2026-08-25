"""Does every control on a screen reach something the engine actually answers?

    python tests/test_screens_and_engine_agree.py

THE FAULT THIS EXISTS TO CATCH
------------------------------
LinkChat was cut out of a bigger program. The screens came over whole, and they
still carry calls to doors that came out in the cut. A call like that does not
crash: the screen asks, the engine says "no such route", the screen catches it
and shows nothing. So the control looks present and does nothing at all, and
nobody finds out until somebody presses it.

It has happened twice, and both were found by hand rather than by a test:

  * the Import box on the Sequences screen — the function behind it was written
    and never wired to a button, so the starter sequence was unreachable
  * the Start browser control — the guide told members to press it, and it does
    not exist on any screen

The walk test cannot catch either. It presses the doors the ENGINE has, so it
proves the engine answers; it cannot know that a screen asks for a door that was
never built.

WHAT IT CHECKS
--------------
1. Every endpoint in api.js that a screen actually calls exists on the engine,
   matched on path AND method. A path that exists but only for POST is still a
   fault if a screen asks for it with GET.
2. Every component file under web/src/components is imported by something. An
   orphan is a control nobody can reach, and a reader who finds it believes the
   feature is there.
3. The guide does not tell a member to press a control that no screen renders.
   That is the fault that would have cost the Wednesday call, and it is a
   documentation fault rather than a code one - which is exactly why no code
   test was looking for it.

Nothing here starts the engine or touches LinkedIn. It reads the route table out
of the app object and the calls out of the screen source.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WEB = ROOT / "web" / "src"

failures: list[str] = []


def check(label: str, ok: bool, why: str = "") -> None:
    print("  %-6s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        for line in str(why).splitlines():
            print("         %s" % line)
        failures.append(label)


# ---------------------------------------------------------------------------
# The engine's real route table, method by method.
# ---------------------------------------------------------------------------
def engine_routes() -> set[tuple[str, str]]:
    """Every path and method the engine really serves.

    Read from the app's own generated description rather than by walking the
    route objects. Walking them misses routers that were included rather than
    declared here, and a missed router reads as a whole screen full of dead
    controls - which is a false alarm loud enough to make the test worthless.
    """
    from engine.server import app

    found: set[tuple[str, str]] = set()
    for path, ops in (app.openapi().get("paths") or {}).items():
        if not path.startswith("/api") or "{rest" in path:
            continue
        for method in ops:
            found.add((method.upper(), path))
    return found


def matches(method: str, path: str, routes: set[tuple[str, str]]) -> bool:
    """A screen's path against the engine's, allowing for {id} placeholders."""
    for m, tpl in routes:
        if m != method:
            continue
        pattern = re.sub(r"\{[^}]+\}", "[^/]+", tpl)
        if re.fullmatch(pattern, path):
            return True
    return False


# ---------------------------------------------------------------------------
# What the screens ask for.
# ---------------------------------------------------------------------------
_VERB = {"get": "GET", "post": "POST", "put": "PUT", "del": "DELETE",
         "patch": "PATCH"}


def screen_calls() -> dict[str, tuple[str, str]]:
    """name -> (METHOD, /api path) for every api.js entry a screen calls."""
    api = (WEB / "api.js").read_text(encoding="utf-8")
    src = ""
    for p in list(WEB.rglob("*.jsx")) + [p for p in WEB.rglob("*.js")
                                         if p.name != "api.js"]:
        src += p.read_text(encoding="utf-8")

    defs: dict[str, tuple[str, str]] = {}

    def harvest(text: str, prefix: str = "") -> None:
        for name, verb, path in re.findall(
                r"(\w+):\s*\([^)]*\)\s*=>\s*(get|post|put|del|patch)\("
                r"\s*[`\"']([^`\"']+)", text):
            defs[prefix + name] = (_VERB[verb], path)

    harvest(api)
    for group, body in re.findall(r"^\s{2}(\w+):\s*\{(.*?)^\s{2}\},",
                                  api, re.M | re.S):
        harvest(body, group + ".")

    called = {}
    for name, spec in defs.items():
        root = name.split(".")[0]
        if ("api.%s" % name) in src or ("api.%s." % root) in src:
            called[name] = spec
    return called


def main() -> int:
    print("=" * 72)
    print("  do the screens and the engine agree?")
    print("=" * 72)

    routes = engine_routes()
    check("the engine has a route table to compare against", bool(routes),
          "no /api routes found on the app object at all")
    if not routes:
        return 1

    # --- 1. Every call a screen makes must reach a real door ---------------
    calls = screen_calls()
    check("the screens call at least something", bool(calls),
          "no api.js entries appear in any screen")

    dead = []
    for name, (method, path) in sorted(calls.items()):
        probe = "/api" + re.sub(r"\$\{[^}]*\}", "1", path).split("?")[0]
        if not matches(method, probe, routes):
            dead.append("api.%s -> %s %s" % (name, method, probe))
    check("every control on a screen reaches a door the engine answers (%d checked)"
          % len(calls),
          not dead,
          "these ask for something that is not there:\n" + "\n".join(dead))

    # --- 2. No orphan components ------------------------------------------
    src = ""
    for p in list(WEB.rglob("*.jsx")) + list(WEB.rglob("*.js")):
        src += p.read_text(encoding="utf-8")
    # Components AND the plain helper files beside them. meta.js was an orphan
    # too, and checking only components/*.jsx missed it - so the sweep covers
    # every screen file except the entry points and the shared api client.
    ENTRY = {"main.jsx", "App.jsx", "api.js"}
    candidates = sorted((WEB / "components").glob("*.jsx")) + \
        sorted(p for p in WEB.glob("*.js") if p.name not in ENTRY)
    orphans = []
    for comp in candidates:
        stem = comp.stem
        # Imported by name (a component) or by file (a helper's named exports).
        by_name = re.search(r"import\s+%s\b" % re.escape(stem), src)
        by_file = re.search(r'from\s+["\'][^"\']*%s(\.js)?["\']' % re.escape(stem), src)
        if not (by_name or by_file):
            orphans.append(comp.name)
    check("no screen part is left over and unreachable",
          not orphans,
          "never imported by anything, so nobody can reach them: "
          + ", ".join(orphans))

    # --- 3. The guide must not name a control that is not rendered --------
    # Explicit rather than clever. A regex over bold text missed the real one
    # ("**Press Start browser**" bolds the verb too), and a check that cannot
    # see the fault it was written for is worse than no check.
    # The guide says "press **X**". X has to be a real button, spelled the way
    # the button is spelled.
    #
    # This used to search the whole screen source for the word, which passes on
    # any stray mention: "Activate" appeared in a comment and in a message, so
    # the guide could say press **Activate** while the button actually read
    # "Start using this one" and nothing noticed. Checking against the real
    # button TEXT is the only version of this check that works. Orphaned files
    # are excluded - they carry their own button text and would vouch for
    # controls nobody can reach, which is the fault vouching for itself.
    button_text: set[str] = set()
    for p in list(WEB.rglob("*.jsx")):
        if p.name in orphans:
            continue
        body = p.read_text(encoding="utf-8")
        for raw in re.findall(r"<button\b[^>]*>(.*?)</button>", body, re.S):
            # A label is often inside an expression rather than beside it:
            #   {sync.running ? "Syncing…" : "↻ Sync inbox"}
            # Dropping expressions wholesale threw those away, and the guide
            # could then name a real button and be told it did not exist.
            # So: take the quoted strings out of the expression FIRST, then
            # take the plain text around it.
            for lit in re.findall(r'"([^"\n]{2,40})"|\'([^\'\n]{2,40})\'', raw):
                s = (lit[0] or lit[1]).strip()
                if s and not s.startswith(("http", "/", "#")) and " " in s or (
                        s and s.isalpha()):
                    button_text.add(s)
            txt = re.sub(r"\{[^{}]*\}", " ", raw)
            txt = re.sub(r"<[^>]*>", " ", txt)
            txt = " ".join(txt.split())
            if txt:
                button_text.add(txt)

    check("the screens have buttons to compare the guide against",
          len(button_text) >= 5,
          "only found %d button labels, so this check would pass on anything"
          % len(button_text))

    guides = list((ROOT / "guide").glob("*.md"))
    named_but_absent = []
    for g in guides:
        text = g.read_text(encoding="utf-8", errors="replace")
        for label in re.findall(r"press \*\*([^*]{2,40})\*\*", text, re.I):
            label = label.strip()
            if not any(label.lower() in b.lower() for b in button_text):
                named_but_absent.append(
                    "%s says press '%s' and no button reads that" % (g.name, label))
    check("the guide never tells a member to press a control that is not there",
          not named_but_absent,
          "\n".join(sorted(set(named_but_absent))))

    print()
    if failures:
        print("NOT CLEAN: %d" % len(failures))
        return 1
    print("Every control reaches a real door, nothing is orphaned, and the guide")
    print("describes the screens that exist. Clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
