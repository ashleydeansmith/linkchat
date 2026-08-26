# -*- coding: utf-8 -*-
"""check_overflow.py — catch content the guide silently deletes.

For future Claude: run this after ANY edit to the guide, before the PDF goes to
anybody. It is the same check the CRM series uses, and it exists for the same
reason: the pages are a fixed A4 height with `overflow:hidden`, so anything past
the bottom of a page does not reflow onto the next one — it vanishes, and the
render looks perfectly healthy.

Comparing the PDF's text to the HTML's text does not catch it either: ligatures
and line reflow make that comparison too noisy to trust. This measures the render
directly. Each `.page` is laid out in a headless browser at true A4 and the
height its content WANTS is compared against the height it is ALLOWED.

    python check_overflow.py

Exit code is 1 if anything overflows, so it can gate a send.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

HTML = Path(__file__).resolve().parent / "guide.html"

# For a page whose content FITS, the wanted height is always exactly the allowed
# height — so there is no way to read "how much room is left" off a healthy page.
# Only positive overflow carries information. Do not add a "spare room" figure:
# it would read as a safety margin while being identically zero every time.
MEASURE = """
() => Array.from(document.querySelectorAll('.page')).map((p, i) => ({
  index: i + 1,
  allowed: p.clientHeight,
  wanted: p.scrollHeight,
  overflow: p.scrollHeight - p.clientHeight,
  tail: (p.innerText || '').trim().slice(-70).replace(/\\s+/g, ' '),
}))
"""


def main():
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True)
        pg = br.new_page(viewport={"width": 1240, "height": 1754})
        pg.goto(HTML.as_uri(), wait_until="networkidle")
        pg.emulate_media(media="print")
        pages = pg.evaluate(MEASURE)
        br.close()

    print("=" * 72)
    print("  does every page fit inside itself?")
    print("=" * 72)
    bad = [p for p in pages if p["overflow"] > 1]
    for p in pages:
        mark = "over" if p["overflow"] > 1 else "ok"
        print("  %-5s page %-3d  allowed %5d  wanted %5d"
              % (mark, p["index"], p["allowed"], p["wanted"]))
        if p["overflow"] > 1:
            print("         %d px past the bottom. Last words that survive: ...%s"
                  % (p["overflow"], p["tail"]))
    print()
    if bad:
        print("NOT CLEAN: %d page(s) delete content a reader never sees." % len(bad))
        return 1
    print("Every page fits. Nothing is being cut off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
