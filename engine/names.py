"""names.py - turn a LinkedIn display name into the name a human would type.

WHY THIS EXISTS (2026-08-20). People decorate the LinkedIn first-name field on
purpose, and some of them do it as a trap. Mikko Rissanen's first-name field reads
"[palm]Mikko J.[coconut]"; he posted publicly that he keeps the emoji there because
automated senders copy the name field verbatim, so a message opening "Hi
[palm]Mikko J.[coconut]" tells him instantly that nobody read his profile. He is not
alone - there is a whole practice of it. Typing the decoration back at someone is a
self-report that we are a machine, and it costs us the reply.

The same cleaning also fixes the plain-courtesy version of the fault: "Hi TEDDY",
"Hi Sachintha (Sachy)", "Hi Dr." (a live send on 2026-07-14 greeted a doctor as
"Hi Dr.").

WHAT IT REMOVES, and nothing else:
  - pictographs, flags, skin-tone modifiers, keycaps, zero-width joiners and
    variation selectors - the characters that make an emoji
  - bracketed inserts:            "Sachintha (Sachy) Abeyrathne"
  - a credential or tagline tail: "Teddy James, CAIA" / "Ann | Growth Coach"
  - honourifics:                  "Dr. Victor Adekunle"
  - shouting:                     "TEDDY" -> "Teddy"

WHAT IT KEEPS. Letters in every script (Chinese, Cyrillic, Arabic and Greek names
survive untouched), accents in both composed and decomposed form, hyphens
("Anne-Marie") and apostrophes ("O'Brien").

FAIL-SAFE. A name that is nothing but decoration cleans to an empty string, and
every caller already falls back to "there" or drops the message rather than sending
a broken greeting. Empty is the safe answer here, never a guess.

MIRRORED at the automation folder/name_clean.py - the two codebases send on the same
LinkedIn account, so they must clean a name the same way. Change both, and both
test files (the parent program tests/test_name_decoration.py, the automation folder
automation/tests/test_name_clean.py) share one corpus.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = ["clean_name", "first_name_of", "strip_symbols", "was_decorated",
           "decoration_classes", "is_trap", "leaked_decoration"]

# Honourifics that must never become the greeting.
_HONORIFICS = {
    "dr", "mr", "mrs", "ms", "miss", "mx", "prof", "professor", "sir", "dame",
    "lord", "lady", "rev", "reverend", "fr", "capt", "captain", "col", "maj",
    "sgt", "lt", "hon", "eng", "ing", "adv", "arch", "rabbi", "imam", "pastor",
}

# Unicode general categories that carry no name: pictographs and symbols (So),
# modifier symbols incl. skin tones (Sk), format/joiner characters (Cf), enclosing
# marks incl. keycaps (Me), private use (Co) and surrogates (Cs).
_DROP_CATEGORIES = {"So", "Sk", "Cf", "Me", "Co", "Cs"}

# Variation selectors are category Mn, the same category as the accent on "e" in
# Renee written in decomposed form. Naming them explicitly means accents live and
# the emoji presentation selector dies.
_DROP_CODEPOINTS = (
    set(range(0xFE00, 0xFE10))       # variation selectors 1-16
    | set(range(0xE0100, 0xE01F0))   # variation selectors supplement
    | set(range(0x200B, 0x2010))     # zero-width space .. right-to-left mark
    | set(range(0x2060, 0x2070))     # word joiner .. invisible operators
    | {0xFEFF, 0x00AD}               # byte-order mark, soft hyphen
)

_BRACKETED = re.compile(r"[(\[{][^)\]}]*[)\]}]")

# A tagline starts here. The hyphen is deliberately absent - "Anne-Marie" is a name.
_TAGLINE = re.compile(r"\s*[|/\:;<>~*#@+=^\"·•–—«»°].*$", re.S)

# "Teddy James, CAIA" - everything from the first comma is a credential or job title.
_CRED_TAIL = re.compile(r",.*$", re.S)

# What survives as part of a name: any letter or mark, plus hyphen, apostrophe, dot.
_KEEPABLE = re.compile(r"[^\w\s'’.\-]", re.UNICODE)


def _strip_decoration(s: str) -> str:
    out = []
    for ch in s:
        if ord(ch) in _DROP_CODEPOINTS:
            continue
        if unicodedata.category(ch) in _DROP_CATEGORIES:
            continue
        out.append(ch)
    return "".join(out)


def _deshout(tok: str) -> str:
    """"TEDDY" -> "Teddy". Left alone at three characters or fewer, because "JP",
    "TJ" and "AJ" are how those people actually write their names."""
    if len(tok) > 3 and tok.isupper() and any(c.isalpha() for c in tok):
        return tok[0] + tok[1:].lower()
    return tok


def strip_symbols(raw: str | None) -> str:
    """Symbols and joiners removed, everything else left alone. For the fields that
    are typed into a message but are not names - company, title, location - where a
    comma or a slash is real content ("Acme, Inc", "Head of Sales, EMEA") and cutting
    at it would destroy the value."""
    s = _strip_decoration(unicodedata.normalize("NFC", (raw or "").strip()))
    s = re.sub(r"\s+", " ", s)
    # Removing a symbol leaves a hole. "Acme [rocket], Inc" must not become
    # "Acme , Inc", and "Ann - [rocket]" must not become "Ann -".
    s = re.sub(r"\s+([,.;:!?)\]}])", r"\1", s)
    s = re.sub(r"([(\[{])\s+", r"\1", s)
    return s.strip().strip("-,;:|/·•–— ").strip()


def clean_name(raw: str | None) -> str:
    """The full display name with the decoration removed. Empty when nothing real
    is left."""
    s = unicodedata.normalize("NFC", (raw or "").strip())
    s = _strip_decoration(s)
    s = _BRACKETED.sub(" ", s)
    s = _CRED_TAIL.sub("", s)
    s = _TAGLINE.sub("", s)
    s = _KEEPABLE.sub(" ", s)
    toks = []
    for tok in s.split():
        tok = tok.strip(".-'’")
        if not tok or not any(c.isalpha() for c in tok):
            continue
        if tok.rstrip(".").lower() in _HONORIFICS:
            continue
        toks.append(_deshout(tok))
    return " ".join(toks)


def first_name_of(full_name: str | None) -> str:
    """The one word to greet someone by. Empty when the field holds no real name -
    callers fall back to "there" or send nothing."""
    cleaned = clean_name(full_name)
    return cleaned.split()[0] if cleaned else ""


# --- what kind of decoration, so the alarm can stay honest --------------------
# "Lianne P." is NOT decoration. LinkedIn itself abbreviates the surname of a 2nd-
# and 3rd-degree connection, so 410 of 10,305 leads carry a trailing initial that
# nobody chose and that changes no greeting. Counting those as a fault buries the
# 213 that are real. Every class below is a name a person typed on purpose.
_ABBREV_TAIL = re.compile(r"^[^\W\d_]\.?$", re.UNICODE)
_TAGLINE_CHARS = re.compile(r"[|/\:;<>~*#@+=^\"\u00b7\u2022\u2013\u2014]")


def decoration_classes(raw: str | None) -> list[str]:
    """Every kind of decoration on this display name, sorted. Empty list = clean.

      symbol      emoji, flag, skin tone, keycap or joiner     "Petar Dimov [fire]"
      honourific  a title where the name should be             "Dr. Nihir Vedd"
      credential  a qualification or job title after a comma   "Nicole Farah, MSc"
      bracketed   a nickname or note in brackets               "Sarah (Milella) O'Sullivan"
      tagline     a pitch after a separator                    "Ann | Growth Coach"
      shouting    the name in capitals                         "TEDDY James"

    "symbol" is the one that means somebody set a trap on purpose - see the module
    docstring. The rest are ordinary courtesy faults that read just as badly.
    """
    s = (raw or "").strip()
    if not s:
        return []
    found = []
    if s != _strip_decoration(s):
        found.append("symbol")
    toks = s.split()
    if toks and toks[0].rstrip(".").lower() in _HONORIFICS:
        found.append("honourific")
    if "," in s:
        found.append("credential")
    if _BRACKETED.search(s):
        found.append("bracketed")
    if _TAGLINE_CHARS.search(s):
        found.append("tagline")
    if any(len(t) > 3 and t.isupper() and any(c.isalpha() for c in t) for t in toks):
        found.append("shouting")
    return sorted(found)


def is_trap(raw: str | None) -> bool:
    """True when the name carries a symbol somebody chose to put there. This is the
    one that gets an account marked as automated on sight."""
    return "symbol" in decoration_classes(raw)


def leaked_decoration(text: str | None, raw_name: str | None) -> str | None:
    """The piece of THIS PERSON'S decorated name that leaked into an outgoing message,
    or None when nothing did.

    Deliberately narrow. It does NOT ban symbols in outgoing text - the operator's own
    templates may carry an emoji, and a blanket ban would refuse his own writing. It
    asks one question: does a decorated chunk of the name we were handed appear, as
    written, in what we are about to send. That is the only pattern that says "this
    was copied from a profile field".

    Returns the offending substring so the refusal can name it.
    """
    t = text or ""
    raw = (raw_name or "").strip()
    if not t or not raw:
        return None
    if raw != _strip_decoration(raw) and raw in t:
        return raw
    for tok in raw.split():
        if tok != _strip_decoration(tok) and tok in t:
            return tok
    return None


def was_decorated(raw: str | None) -> bool:
    """True when cleaning actually changed the name. For logging, so a run can show
    how often we were about to type decoration at somebody."""
    return (raw or "").strip() != clean_name(raw)
