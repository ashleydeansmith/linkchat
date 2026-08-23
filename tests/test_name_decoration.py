"""Falsifying tests for the decorated-name fix (2026-08-20).

THE FAULT. LinkedIn's first-name field is free text, and a growing number of people
put emoji in it deliberately as a trap. Mikko Rissanen (first-name field
"[palm]Mikko J.[coconut]") posted publicly on 2026-08-20 that he keeps the emoji
there precisely because automated senders copy the field verbatim, so any message
opening "Hi [palm]Mikko J.[coconut]" identifies itself as a machine before he has
read the second line. The program LinkChat was built from did exactly that - its own test used to
PIN the behaviour with the case ("[phone]Peter Chen[llama]" -> "[phone]Peter",
"emoji names pass through"). That assertion is now inverted.

Names below are written as escapes, not literal emoji, so the file survives any
console codepage. This corpus is MIRRORED in Nexus/automation/tests/test_name_clean.py -
the two codebases send on the same LinkedIn account, so they must agree.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from engine.names import (clean_name, decoration_classes, first_name_of,  # noqa: E402
                            is_trap, leaked_decoration, strip_symbols, was_decorated)

FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f" :: {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


# (raw display name, expected greeting, what it proves)
CORPUS = [
    # --- the trap itself -----------------------------------------------------
    ("\U0001F3DDMikko J.\U0001F965 Rissanen\U0001F3DD", "Mikko",
     "the live trap: emoji glued to the first name with no space"),
    ("☎️Peter Chen\U0001F999", "Peter",
     "was PINNED as pass-through in test_drip_sends; now stripped"),
    ("Hannah Wright \U0001F51C Develop Brighton", "Hannah",
     "pictograph used as a separator mid-name"),
    ("Vlad Pent \U0001F1EC\U0001F1E7\U0001F1FA\U0001F1E6", "Vlad",
     "regional-indicator flags are two codepoints each"),
    ("Sarah \U0001F469‍\U0001F4BB Jones", "Sarah",
     "zero-width joiner sequence in the middle"),
    ("Tom \U0001F44B\U0001F3FD Blake", "Tom",
     "skin-tone modifier is a separate codepoint"),
    ("Amy 1️⃣ Scott", "Amy",
     "keycap: digit + variation selector + enclosing mark"),

    # --- the courtesy faults the same clean fixes ----------------------------
    ("Dr. Victor Adekunle", "Victor", "the live 'Hi Dr.' send of 2026-07-14"),
    ("Dr Sarah Jones", "Sarah", "honourific with no dot"),
    ("Prof. Jim Long", "Jim", "Prof"),
    ("Mr T", "T", "honourific plus a single-letter name"),
    ("Teddy James, CAIA", "Teddy", "credential after a comma"),
    ("Sachintha (Sachy) Abeyrathne", "Sachintha", "parenthesised nickname"),
    ("Ann | Growth Coach", "Ann", "tagline after a pipe"),
    ("Ben - Scaling Founders", "Ben", "hyphen tagline still yields the name"),
    ("TEDDY James", "Teddy", "shouting de-shouted"),

    # --- what must NOT be touched -------------------------------------------
    ("Victor Adekunle", "Victor", "a plain name passes through untouched"),
    ("Anne-Marie Dupont", "Anne-Marie", "hyphenated first name kept whole"),
    ("Seán O'Brien", "Seán", "apostrophe and accent kept"),
    ("Renée Zellweger", "Renée", "decomposed accent survives (NFC, not a stripped mark)"),
    ("张伟", "张伟", "Chinese name untouched"),
    ("Анна Петрова", "Анна",
     "Cyrillic name untouched"),
    ("محمد علي", "محمد",
     "Arabic name untouched"),
    ("JP Morgan", "JP", "two-letter initials are NOT de-shouted"),
    ("AJ Cole", "AJ", "three characters or fewer stays as written"),

    # --- fail safe: empty, never a guess ------------------------------------
    ("\U0001F965\U0001F965", "", "a name that is only decoration cleans to empty"),
    ("Dr.", "", "honourific-only degrades to empty, never 'Hi Dr.'"),
    ("", "", "empty stays empty (caller falls back to 'there')"),
    (None, "", "None stays empty"),
]

print("=== first_name_of: the name a human would type ===")
for raw, want, label in CORPUS:
    got = first_name_of(raw)
    check(label, got == want, f"{raw!r} -> {got!r}, wanted {want!r}")

print("\n=== clean_name: the full name, decoration removed ===")
FULL = [
    ("\U0001F3DDMikko J.\U0001F965 Rissanen\U0001F3DD", "Mikko J Rissanen"),
    ("☎️Peter Chen\U0001F999", "Peter Chen"),
    ("Teddy James, CAIA", "Teddy James"),
    ("Sachintha (Sachy) Abeyrathne", "Sachintha Abeyrathne"),
    ("\U0001F965\U0001F965", ""),
]
for raw, want in FULL:
    got = clean_name(raw)
    check(f"clean_name {raw!r}", got == want, f"got {got!r}, wanted {want!r}")

print("\n=== strip_symbols: company / title / location keep their commas ===")
FIELDS = [
    ("Acme \U0001F680, Inc", "Acme, Inc", "a comma in a company name is real content"),
    ("Head of Sales, EMEA", "Head of Sales, EMEA", "a job title with a comma is untouched"),
    ("London \U0001F1EC\U0001F1E7", "London", "flag dropped, city kept"),
    ("\U0001F525 Founder \U0001F525", "Founder", "leading and trailing decoration"),
    ("Ann - \U0001F680", "Ann", "no dangling separator left behind"),
    (None, "", "None is empty"),
]
for raw, want, label in FIELDS:
    got = strip_symbols(raw)
    check(label, got == want, f"{raw!r} -> {got!r}, wanted {want!r}")

print("\n=== was_decorated: the run can report how often this fires ===")
check("decorated name reports True", was_decorated("☎️Peter Chen\U0001F999") is True)
check("plain name reports False", was_decorated("Peter Chen") is False)

print("\n=== decoration_classes: names LinkedIn abbreviated are NOT a fault ===")
CLASSES = [
    ("Lianne P.", [], "LinkedIn's own 2nd-degree surname abbreviation - 410 leads carry one"),
    ("Yousef .", [], "the same, with the initial missing"),
    ("Victor Adekunle", [], "a plain name is clean"),
    ("\U0001F312 Andre Wang", ["symbol"], "the trap"),
    ("Dr. Nihir Vedd", ["honourific"], "a title where the name should be"),
    ("Nicole Farah, MSc", ["credential"], "a qualification after a comma"),
    ("Sachintha (Sachy) Abeyrathne", ["bracketed"], "a nickname in brackets"),
    ("Ann | Growth Coach", ["tagline"], "a pitch after a separator"),
    ("TEDDY James", ["shouting"], "capitals"),
    ("\U0001F4F8 JULIEANN Daly MSc", ["shouting", "symbol"], "two kinds at once, sorted"),
]
for raw, want, label in CLASSES:
    got = decoration_classes(raw)
    check(label, got == want, f"{raw!r} -> {got}, wanted {want}")
check("is_trap only fires on a symbol",
      is_trap("\U0001F312 Andre Wang") and not is_trap("Dr. Nihir Vedd"))

print("\n=== leaked_decoration: refuses THEIR decoration, never Ashley's own ===")
LEAKS = [
    ("Hey \u26a1\ufe0fJames, good to connect", "\u26a1\ufe0fJames Marley\u26a1\ufe0f",
     "\u26a1\ufe0fJames", "the message on file as SENT - this is the one that got out"),
    ("Hi ☎️Peter", "☎️Peter Chen\U0001F999", "☎️Peter", "leak caught"),
    ("Hey James, good to connect", "\u26a1\ufe0fJames Marley\u26a1\ufe0f", None,
     "cleaned greeting passes"),
    ("Hey James \U0001F680 hope you are well", "James Marley", None,
     "an emoji ASHLEY wrote in his own template is NOT refused"),
    ("Hi Peter", "☎️Peter Chen\U0001F999", None, "clean greeting to a decorated lead passes"),
    ("Hi Peter", None, None, "no name, nothing to leak"),
    ("", "☎️Peter", None, "empty message"),
]
for text, raw, want, label in LEAKS:
    got = leaked_decoration(text, raw)
    check(label, got == want, f"got {got!r}, wanted {want!r}")


print("\n=== LinkChat's copy has not drifted from the one it came from ===")
# LinkChat and LinkForge act on LinkedIn to the same house standard, and each
# carries its OWN copy of names.py because the convention here is to port by copy
# rather than import across folders. Copies drift silently. This is the check that
# makes drift fail a test instead of reaching a person.
CANON = pathlib.Path.home() / "Documents" / "LinkForge" / "linkforge" / "names.py"
MINE = pathlib.Path(__file__).resolve().parents[1] / "engine" / "names.py"
mine = MINE.read_text(encoding="utf-8")
if CANON.exists():
    canon = CANON.read_text(encoding="utf-8")
    check("engine/names.py matches the copy it was ported from",
          mine[mine.index("from __future__"):] == canon[canon.index("from __future__"):],
          "a copy has drifted - re-mirror it, then run both name tests")
else:
    # A member's machine has no LinkForge folder, and that is not a fault of
    # theirs. The check is for this machine; elsewhere it says so and moves on.
    print("  ---   the copy it was ported from is not on this machine; nothing to compare")

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
