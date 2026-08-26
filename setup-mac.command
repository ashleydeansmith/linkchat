#!/bin/bash
# ---------------------------------------------------------------------------
#  LinkChat - setup, for a Mac
#
#  Run this once. It installs what LinkChat needs and puts a LinkChat file on
#  your desktop. It does not touch LinkedIn and it does not ask for a password.
#
#  ⚠ NOBODY HAS EVER RUN LINKCHAT ON A MAC. This file was written on 2026-08-25
#  and has never been run on the computer it is for. It may not work. If it
#  stops, send whoever gave you this everything it printed - that is useful, not a
#  nuisance, and it is how the Mac version gets finished.
#
#  Two faults this file is written against, both of which are the Mac versions
#  of faults already found on Windows:
#
#  1. A Mac has a file called python3 that is not Python. Typing python3 on a
#     Mac with no developer tools installed opens a box asking you to install
#     them. So this asks Python for its version and only believes an answer
#     that starts "Python 3".
#
#  2. The Python that comes with a Mac REFUSES to have anything installed into
#     it - it answers "externally-managed-environment" and stops. So LinkChat
#     builds its own private Python inside its own folder and installs there.
#     Nothing outside the LinkChat folder is touched or changed.
# ---------------------------------------------------------------------------
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

# --- Is this actually a Mac? ----------------------------------------------
# Somebody on Windows who opens this in Git Bash gets a half-install with no
# desktop icon and no error naming the cause. Say it instead.
KIND="$(uname -s 2>/dev/null || echo unknown)"
case "$KIND" in
  Darwin) ;;
  MINGW*|MSYS*|CYGWIN*)
    echo
    echo "  This is the Mac installer and this is a Windows computer."
    echo
    echo "  Close this and double-click  setup.cmd  instead."
    echo
    read -r -p "  Press return to close. " _ || true
    exit 1 ;;
  *)
    echo
    echo "  This computer reports itself as: $KIND"
    echo
    echo "  LinkChat has been run on Windows only. This installer was written"
    echo "  for a Mac and has never been run on one. It may work here and"
    echo "  nobody knows. Ask whoever gave you this before you rely on it."
    echo
    read -r -p "  Press return to carry on anyway, or close this window. " _ || true
    ;;
esac

echo
echo "  Setting up LinkChat. This takes about five minutes, most of it downloading."
echo

# --- Is there a real Python 3.10 or later on this Mac? ---------------------
PY=""
for CANDIDATE in python3.13 python3.12 python3.11 python3.10 python3; do
  FOUND="$(command -v "$CANDIDATE" 2>/dev/null)" || continue
  [ -n "$FOUND" ] || continue
  # Ask it its version. A Mac stub that only offers to install developer tools
  # does not answer this, so an empty answer is a no.
  VER="$("$FOUND" -c 'import sys;print("Python %d.%d" % sys.version_info[:2])' 2>/dev/null)" || continue
  case "$VER" in
    "Python 3."*) ;;
    *) continue ;;
  esac
  MINOR="${VER#Python 3.}"
  if [ "$MINOR" -ge 10 ] 2>/dev/null; then
    PY="$FOUND"
    echo "  Found $VER at $PY"
    break
  fi
done

if [ -z "$PY" ]; then
  cat <<'MSG'

  There is no Python 3.10 or later on this Mac that LinkChat can use.

  A Mac has a file called python3 that is not Python - typing it opens a box
  offering to install developer tools. That is not Python and LinkChat cannot
  use it.

  Get the real one, free, from https://www.python.org/downloads/macos/
  Download the 64-bit universal2 installer, run it, then close this window,
  open the LinkChat folder again and double-click this file again.

MSG
  read -r -p "  Press return to close. " _ || true
  exit 1
fi
echo

# --- LinkChat's own private Python, inside its own folder ------------------
echo "  [1 of 5] Making LinkChat its own private Python..."
if [ ! -x ".venv/bin/python" ]; then
  "$PY" -m venv .venv || { echo; echo "  Could not make it. Send the lines above to whoever gave you this."; read -r -p "  Press return to close. " _ || true; exit 1; }
fi
VPY=".venv/bin/python"
[ -x "$VPY" ] || { echo "  The private Python is not there. Send the lines above to whoever gave you this."; read -r -p "  Press return to close. " _ || true; exit 1; }

echo "  [2 of 5] Installing the parts LinkChat needs..."
"$VPY" -m pip install --quiet --upgrade pip || { echo; echo "  That did not finish. Send the lines above to whoever gave you this."; read -r -p "  Press return to close. " _ || true; exit 1; }
"$VPY" -m pip install --quiet -r requirements.txt || { echo; echo "  That did not finish. Send the lines above to whoever gave you this."; read -r -p "  Press return to close. " _ || true; exit 1; }

# The window itself needs Mac-only parts that a Windows computer has no use for,
# so they are not in the parts list. Without them LinkChat still runs - it opens
# in your ordinary browser instead of its own window.
echo "  [3 of 5] Installing the parts that draw the window on a Mac..."
"$VPY" -m pip install --quiet "pywebview[cocoa]>=5.0" || echo "  (The window parts did not install. LinkChat will open in your browser instead.)"

echo "  [4 of 5] Downloading the browser LinkChat reads with (about 150 MB)..."
"$VPY" -m playwright install chromium || { echo; echo "  That did not finish. Send the lines above to whoever gave you this."; read -r -p "  Press return to close. " _ || true; exit 1; }

echo "  [5 of 5] Checking the parts actually landed, and putting LinkChat on your desktop..."
# playwright.sync_api, not playwright: the outer name imports even when the
# half that drives a browser is missing.
"$VPY" -c "import fastapi, uvicorn, pydantic, playwright.sync_api, psutil" || {
  echo
  echo "  The parts installed but Python cannot find them."
  echo "  Send the last few lines above to whoever gave you this."
  read -r -p "  Press return to close. " _ || true
  exit 1
}

DESKTOP="$HOME/Desktop"
[ -d "$DESKTOP" ] || DESKTOP="$HOME"
LAUNCHER="$DESKTOP/LinkChat.command"
cat > "$LAUNCHER" <<LAUNCH
#!/bin/bash
# Opens LinkChat. Made by setup.command - do not edit.
cd "$HERE" || exit 1
exec "$HERE/.venv/bin/python" -m engine desktop
LAUNCH
chmod +x "$LAUNCHER" || true

echo
echo "  Done. There is now a LinkChat file on your desktop."
echo "  Double-click it. It will ask where your CRM folder is."
echo
echo "  The first time you double-click it, a Mac may say it cannot be opened"
echo "  because it is from an unidentified developer. Right-click it instead,"
echo "  choose Open, and then choose Open again in the box that appears."
echo
read -r -p "  Press return to close. " _ || true
exit 0
