"""Every address printed in the PDF, fetched for real.

Two questions per address, not one: is the address intact after the page
renderer has had it, and is the thing it points at actually there. Checking only
the first is how a link that reads correctly and leads nowhere gets sent.
"""
import pathlib
import re
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# The PDF beside this folder, in the repo's own guide/ (a path on one computer is not shipped).
PDF = pathlib.Path(__file__).resolve().parent.parent / "LinkChat - Install and Walk Through.pdf"

# Read the words with a real PDF reader. Pulling them out of the compressed
# streams by hand found nothing at all, and "no addresses found" from a document
# that plainly carries one is a checker reporting clean because it is broken.
from pypdf import PdfReader

reader = PdfReader(str(PDF))
print("pages:", len(reader.pages))
text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
print("characters read:", len(text))

# The ending must not run straight into more letters. Without that,
# `setup-mac.command` - a filename, not an address - was read as `setup-mac.com`
# and reported as a dead link, which would have failed a document whose every
# real address was fine.
ADDRESS = (r"(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9.-]*"
           r"\.(?:com|org|uk|io|dev|net)(?![a-z])(?:/[^\s)\"']*)?")
found = sorted({a.rstrip(".,") for a in re.findall(ADDRESS, text, re.I)})

print("\naddresses printed in the document:")
if not found:
    print("  none found - which is itself worth checking, because the document does "
          "carry one")
    sys.exit(1)

bad = 0
for a in found:
    url = a if a.lower().startswith("http") else "https://" + a
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        code = e.__class__.__name__
    ok = code == 200
    if not ok:
        bad += 1
    print("  %-6s %-48s %s" % ("ok" if ok else "FAIL", a, code))

print()
print("every address in the document resolves" if not bad
      else "*** %d ADDRESS(ES) DO NOT RESOLVE - DO NOT SEND ***" % bad)
sys.exit(1 if bad else 0)
