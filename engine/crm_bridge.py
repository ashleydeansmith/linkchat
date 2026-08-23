"""crm_bridge.py — the join between LinkChat and the CRM you already built.

LinkChat owns no record about a person. Your people live in the CRM you built in
Sessions 0 to 2, and they stay there. This file is the only place that reaches
into it, so there is one door rather than a dozen.

It does four jobs:

  1. Finds your CRM, and loads the parts of it LinkChat has to obey.
  2. Reads your people, and gives each one a key LinkChat can act on.
  3. Asks permission before anything happens, and records it after.
  4. Writes what happened back into your event log.

The four rules it enforces are not LinkChat's rules. They are the ones already
sitting in your `_engine` folder, written when you installed Layer 6:

  - one daily ceiling shared by everything      `limits.py`
  - nobody on the hold list is ever contacted   `holds.py`
  - one job drives the browser at a time        `browser_lock.py`
  - the last few inches are you                 `sendgate.py`

WHEN SOMETHING IS MISSING, THE ANSWER IS NO.
If a member has not reached Layer 6 yet, the part that would grant permission is
absent. An absent guard must never read as an open door, so every check here
answers "no" when it cannot find the code that would have answered properly.
LinkChat still opens and still reads; it just cannot act.
"""
from __future__ import annotations

import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

from . import names

HERE = Path(__file__).resolve().parent
SETTINGS = HERE.parent / "linkchat.json"

# The parts of your CRM LinkChat looks for, and what each one is for.
WANTED = {
    "review":       "the step where you approve, not the sequence",
    "crm_paths":    "where your CRM keeps things",
    "identity":     "telling one person from another",
    "ledger":       "the event log",
    "limits":       "the shared daily ceiling",
    "holds":        "the people nothing automatic may contact",
    "browser_lock": "one job driving the browser at a time",
    "sendgate":     "the last few inches being you",
}

# What LinkChat needs before it may do each kind of work.
#
# Reading needs nothing. A person record is a text file with a header, and the
# reader below falls back to its own when your resolver is not there yet. Saying
# a member cannot read what they can read is how a working program looks broken
# on the call.
NEEDED_FOR = {
    "read":   (),
    "sync":   ("browser_lock",),
    "draft":  ("ledger", "limits", "holds", "sendgate", "review"),
}


class NoCRM(Exception):
    """LinkChat was pointed at a folder that is not a CRM."""


class NotAllowed(Exception):
    """Something asked to act and the answer was no. The reason says why."""


# --------------------------------------------------------------- finding it

def remembered():
    """The CRM folder you chose last time, if you have chosen one."""
    try:
        return Path(json.loads(SETTINGS.read_text(encoding="utf-8"))["crm"])
    except (OSError, ValueError, KeyError):
        return None


def remember(path):
    """Write down which CRM folder to use, so you are asked once and not again."""
    path = Path(path).expanduser().resolve()
    data = {}
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    data["crm"] = str(path)
    tmp = SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS)          # never open the real file for writing
    return path


def looks_like_a_crm(path):
    """Is this a CRM you built, rather than any other folder?

    The test is the engine folder plus a people folder. Both are made by Layer 1,
    so anything that has been through the first installer passes and a folder
    picked by accident does not.
    """
    path = Path(path).expanduser()
    return (path / "_engine").is_dir() and (path / "People").is_dir()


def find():
    """Your CRM, from the first of these that answers: remembered, told, usual place."""
    for candidate in (remembered(),
                      os.environ.get("OUTLIERS_CRM"),
                      Path.home() / "CRM"):
        if candidate and looks_like_a_crm(candidate):
            return Path(candidate).expanduser().resolve()
    return None


# --------------------------------------------------------------- the bridge

class Bridge:
    """One open door into one CRM."""

    def __init__(self, root):
        root = Path(root).expanduser().resolve()
        if not looks_like_a_crm(root):
            raise NoCRM("%s has no _engine and People folder in it" % root)
        self.root = root
        self.engine = root / "_engine"
        self.parts = {}
        self.missing = {}
        self._load_parts()

    def _load_parts(self):
        """Import the parts of the CRM that are there, and note the ones that are not.

        Their own modules find the CRM through this environment variable, so it is
        set before any of them are imported. Setting it afterwards would leave each
        module pointed at whatever it guessed on the way in.

        THE FORGETTING STEP IS THE IMPORTANT ONE. Python keeps every module it has
        already imported and hands the same one back next time it is asked. Open a
        second CRM in the same session and the parts of the FIRST one answer for
        it — so a CRM with no send gate reports that it has one, which is a guard
        reading as present while being absent. Everything loaded from any engine
        folder is dropped first, so each CRM is asked afresh.
        """
        os.environ["OUTLIERS_CRM"] = str(self.root)
        _forget_engine_modules()
        # Stays on the path for as long as this bridge is open: their modules
        # import one another, and some do it at the moment they are called
        # rather than when they are loaded.
        for stale in [p for p in sys.path if p.endswith("_engine")]:
            sys.path.remove(stale)
        sys.path.insert(0, str(self.engine))
        for name, purpose in WANTED.items():
            try:
                module = __import__(name)
            except Exception as exc:
                self.missing[name] = "%s - %s" % (purpose, exc.__class__.__name__)
                continue
            # Loaded, but out of where? Anything that did not come from THIS
            # engine folder belongs to another CRM and does not count as present.
            where = getattr(module, "__file__", "") or ""
            try:
                same = Path(where).resolve().parent == self.engine
            except OSError:
                same = False
            if same:
                self.parts[name] = module
            else:
                self.missing[name] = "%s - not in this CRM" % purpose
        paths = self.parts.get("crm_paths")
        if paths is not None:
            paths.use_vault(self.root)

    # ---------------------------------------------------------- what works

    def can(self, job):
        """May LinkChat do this kind of work? Returns (yes_or_no, what_is_missing)."""
        absent = [n for n in NEEDED_FOR[job] if n not in self.parts]
        if absent:
            return False, [WANTED[n] for n in absent]
        return True, []

    def state(self):
        """One picture of what this CRM can and cannot do, for the screen."""
        jobs = {job: self.can(job)[0] for job in NEEDED_FOR}
        return {
            "crm": str(self.root),
            "found": sorted(self.parts),
            "missing": self.missing,
            "can": jobs,
            "reading_only": not jobs["draft"],
            "cap": self.cap(),
            "used": self.used(),
        }

    # -------------------------------------------------------------- people

    def people(self):
        """Every person in your CRM, with the key LinkChat acts on.

        The key is the LinkedIn address, tidied by your own resolver so that the
        several ways of spelling one profile all land on one person.
        """
        out = []
        folder = self.root / "People"
        if not folder.is_dir():
            return out
        for path in sorted(folder.rglob("*.md")):
            record = self._read_person(path)
            if record:
                out.append(record)
        return out

    def _read_person(self, path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        fields = self._front_matter(text)
        if not fields:
            return None
        url = _first(fields, "linkedin-url", "linkedin", "linkedin_url",
                     "profile-url", "profile")
        full = _first(fields, "name", "full-name", "fullname") or path.stem
        return {
            "key": self.key(url) or "",
            # The name as they wrote it, never rewritten. It is who they are, and
            # a record that quietly corrects somebody's own spelling of their own
            # name is a record you stop being able to trust.
            "name": full,
            # The name a person would actually type at the top of a message. Some
            # people put an emoji in the first-name field on purpose, and say so
            # publicly: a message opening "Hi (palm)Mikko(coconut)" tells them
            # nobody read the profile. So the greeting is worked out once, here,
            # and the raw field is never the thing that gets typed.
            "greet": names.first_name_of(full) or "",
            "decorated": bool(names.was_decorated(full)),
            "url": url or "",
            "email": _first(fields, "email", "email-address", "e-mail") or "",
            "state": _first(fields, "relationship-state", "status", "stage") or "",
            "company": _first(fields, "company", "organisation", "organization") or "",
            "role": _first(fields, "role", "title", "headline", "position") or "",
            "file": str(path),
        }

    def _front_matter(self, text):
        """Your resolver's reader when it is there, a plain one when it is not."""
        identity = self.parts.get("identity")
        if identity is not None and hasattr(identity, "front_matter"):
            try:
                return identity.front_matter(text) or {}
            except Exception:
                pass
        if not text.startswith("---"):
            return {}
        end = text.find("\n---", 3)
        if end < 0:
            return {}
        fields = {}
        for line in text[3:end].splitlines():
            if ":" in line and not line.startswith(("  ", "-", "#")):
                k, _, v = line.partition(":")
                fields[k.strip().lower()] = v.strip().strip("'\"")
        return fields

    def key(self, url):
        """One profile address, spelled one way, so two spellings never become two people."""
        if not url:
            return None
        identity = self.parts.get("identity")
        if identity is not None and hasattr(identity, "slug"):
            try:
                return identity.slug(url)
            except Exception:
                pass
        match = re.search(r"linkedin\.com/in/([^/?#]+)", str(url), re.I)
        return match.group(1).strip().lower() if match else None

    # ------------------------------------------------------- the four rules

    def cap(self):
        limits = self.parts.get("limits")
        try:
            return int(limits.cap_from_config()) if limits else None
        except Exception:
            return None

    def used(self):
        limits = self.parts.get("limits")
        try:
            return int(limits.total()) if limits else None
        except Exception:
            return None

    def may_act(self, kind):
        """May one more action happen today? The ceiling is shared with Gather.

        Missing means no. A ceiling that cannot be consulted is not an absent
        ceiling, and treating it as one is how two programs on one account each
        stay inside a limit while the account goes over it.
        """
        limits = self.parts.get("limits")
        if limits is None:
            return False, "the shared daily ceiling is not installed (Layer 6)"
        try:
            return limits.allow(kind)
        except Exception as exc:
            return False, "the shared daily ceiling could not be read (%s)" % exc.__class__.__name__

    def did_act(self, kind, note=None):
        """Count one action, after it happened and never before."""
        limits = self.parts.get("limits")
        if limits is None:
            return None
        try:
            return limits.record(kind, note)
        except Exception:
            return None

    def is_held(self, *identifiers):
        """Is this person one nothing automatic may contact?

        Missing means held. The whole point of the list is that some people must
        never be written to, so the version of this that cannot read the list has
        to refuse everyone rather than let everyone through.
        """
        holds = self.parts.get("holds")
        if holds is None:
            return True
        try:
            if hasattr(holds, "is_held_person"):
                return bool(holds.is_held_person(*identifiers))
            return bool(holds.is_held(*identifiers))
        except Exception:
            return True

    @contextmanager
    def browser(self, owner="linkchat"):
        """Hold the browser for the length of one job, then let it go.

        Two jobs in one browser profile fight over the same login. The damage
        does not arrive as a crash; it arrives days later as an account that
        stops trusting the session.
        """
        lock = self.parts.get("browser_lock")
        if lock is None:
            raise NotAllowed("the browser lock is not installed (Layer 6)")
        try:
            token = lock.acquire(owner)
        except Exception as exc:
            raise NotAllowed("something else is driving the browser (%s)"
                             % exc.__class__.__name__)
        try:
            yield token
        finally:
            try:
                lock.release(token)
            except Exception:
                pass

    # The three steps a message goes through, and why there are three.
    #
    # A sequence writes a message. The same sequence must not then decide the
    # message is good, because the reasoning that wrote it is the reasoning that
    # would have to find the fault in it, and to that reasoning it looks right.
    # So: the sequence PROPOSES, you APPROVE on the screen, and only then does it
    # go to your outbox for you to send. Three steps, three different hands.

    AUTHOR = "linkchat-sequence"

    def propose(self, item_id, body, summary="", to="", identifier="",
                thread_urn=""):
        """A sequence has written a message. Record who wrote it, and wait.

        Who it is FOR is stored alongside it, because the screen that shows you
        the message has to say who it would go to. A message you cannot attribute
        to a person is one you cannot judge.

        The conversation it belongs to is stored too. Without it there is nowhere
        to put the message when you approve it, and the loop stops at your outbox.
        """
        review = self.parts.get("review")
        if review is None:
            raise NotAllowed("the review step is not installed (Layer 6)")
        return review.submit(item_id, self.AUTHOR, summary or body[:80],
                             {"body": body, "to": to, "identifier": identifier,
                              "thread_urn": thread_urn})

    def approve(self, item_id, reviewer=None):
        """You, on the screen, saying this one may go. Never the sequence itself."""
        review = self.parts.get("review")
        if review is None:
            raise NotAllowed("the review step is not installed (Layer 6)")
        reviewer = reviewer or self.you()
        if not reviewer:
            raise NotAllowed("an approval has to name who gave it")
        try:
            return review.approve(item_id, reviewer)
        except Exception as exc:
            raise NotAllowed(str(exc))

    def awaiting_you(self):
        """Everything a sequence has written that you have not looked at yet.

        Who each one is for is lifted to the top level so the screen does not have
        to go digging for it.
        """
        review = self.parts.get("review")
        if review is None:
            return []
        try:
            rows = review.waiting() or []
        except Exception:
            return []
        out = []
        for row in rows:
            item = dict(row)
            payload = item.get("payload") or {}
            item["to"] = payload.get("to") or ""
            item["identifier"] = payload.get("identifier") or ""
            item["thread_urn"] = payload.get("thread_urn") or ""
            out.append(item)
        return out

    def you(self):
        """Your name, for the approval line. Asked once when LinkChat is set up."""
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8")).get("you") or ""
        except (OSError, ValueError):
            return ""

    def stage(self, message):
        """Put a finished message in your outbox, unsent, for you to send.

        This is as far as LinkChat goes with anything carrying your words. It
        runs your own checks first, and refuses rather than staging if any fail.
        """
        gate = self.parts.get("sendgate")
        if gate is None:
            raise NotAllowed("the send gate is not installed (Layer 6)")
        verdict = gate.review_outbound(message)
        if verdict.get("refused"):
            raise NotAllowed("; ".join(verdict.get("reasons") or ["refused"]))
        return gate.stage_for_you(message, verdict)

    # The one check in your send gate that a message you typed cannot pass, and
    # what is done about it.
    #
    # Your gate runs five checks. Four of them apply to anything: is the person
    # identified, are they on the hold list, is there room in the shared daily
    # limit, is there anything written. The fifth is that somebody other than the
    # author approved it - and that one was written for a message a machine wrote.
    # It exists so a machine cannot mark its own homework.
    #
    # A reply YOU typed has no second pair of eyes and cannot have one. You are
    # the author and you are the only person in the room. Faking a record that
    # says otherwise would be a lie in your own files, and quietly turning the
    # check off would be worse - it is the same check that stops a sequence
    # releasing its own work.
    #
    # So: the gate is asked, in full, and its answer is obeyed. If ANYTHING
    # refuses, the message stops. The single exception is the check named below,
    # and only when the words are your own. A check your gate gains later is not
    # on this list, so it stops the message - which is the safe direction for a
    # rule nobody has thought about yet.
    ONLY_CHECK_YOUR_OWN_WORDS_SKIP = "independently-approved"

    def stage_your_own(self, message):
        """Put a message YOU typed into your outbox, and say so in the record.

        Everything your gate refuses still refuses, except the one check above.
        The file written says plainly that you wrote it and you sent it, so a
        month from now the outbox does not read as though a sequence produced it.
        """
        gate = self.parts.get("sendgate")
        if gate is None:
            raise NotAllowed("the send gate is not installed (Layer 6)")
        verdict = gate.review_outbound(message)
        refusals = [c for c in verdict.get("checks") or [] if not c.get("ok")]
        blocking = [c for c in refusals
                    if c.get("id") != self.ONLY_CHECK_YOUR_OWN_WORDS_SKIP]
        if blocking:
            raise NotAllowed("; ".join(c.get("reason", "refused") for c in blocking))

        # Written by hand rather than through gate.stage_for_you, because that
        # function's promise is "every check passed" and here one did not apply.
        # Claiming its promise would make the promise worth less everywhere else.
        paths = self.parts.get("crm_paths")
        if paths is None:
            raise NotAllowed("your CRM cannot say where its outbox is (Layer 1)")
        safe = self.parts.get("_safe_write")
        folder = paths.outbox_dir()
        folder.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        when = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stamp = when.replace(":", "").replace("-", "")
        who = "".join(c for c in str(message.get("to") or "unknown")
                      if c.isalnum() or c in " -_").strip() or "unknown"
        target = folder / ("%s--%s.md" % (stamp, who))
        lines = "\n".join("- %s: %s" % (c.get("id"), c.get("reason"))
                           for c in verdict.get("checks") or [])
        body = (
            "---\n"
            "to: %s\n"
            "identifier: %s\n"
            "kind: %s\n"
            "written-by: you\n"
            "approved-by: you\n"
            "staged: %s\n"
            "sent: false\n"
            "---\n\n"
            "# You wrote this, in Conversations\n\n"
            "These are your own words, typed into your own inbox. The check that "
            "asks whether somebody other than the author approved it does not "
            "apply, because you are the author and there is nobody else. Every "
            "other check your send gate runs was run, and passed.\n\n"
            "---\n\n%s\n\n---\n\n%s\n"
            % (message.get("to", ""), message.get("identifier", ""),
               message.get("kind", "reply"), when,
               str(message.get("body", "")).strip(), lines))
        tmp = target.with_suffix(".md.tmp")
        tmp.write_text(body, encoding="utf-8", newline="\n")
        os.replace(tmp, target)      # never open the real file for writing
        return target

    def would_stage(self, message):
        """Run the checks and report, without writing anything. For the screen."""
        gate = self.parts.get("sendgate")
        if gate is None:
            return {"refused": True,
                    "reasons": ["the send gate is not installed (Layer 6)"],
                    "checks": []}
        try:
            return gate.review_outbound(message)
        except Exception as exc:
            return {"refused": True,
                    "reasons": ["the send gate could not be run (%s)"
                                % exc.__class__.__name__],
                    "checks": []}

    # ------------------------------------------------------------- the log

    def log(self, type_, *identifiers, **kw):
        """Write one line into your event log saying what happened.

        Your log refuses a word it has never been taught, which is what stops one
        occurrence ending up with six names. If it refuses, that is your log
        working, so it is reported rather than swallowed.
        """
        ledger = self.parts.get("ledger")
        if ledger is None:
            return None
        kw.setdefault("source", "linkchat")
        try:
            return ledger.emit_for(type_, *identifiers, **kw)
        except Exception as exc:
            return {"refused": str(exc)}


def _forget_engine_modules():
    """Drop every module already loaded out of any CRM engine folder.

    Python hands back the module it loaded the first time, whoever asks next. In
    a program that can be pointed at a different CRM, that means the last CRM
    keeps answering for the new one. The consequence is not a wrong number on a
    screen: it is a CRM with no send gate reporting that it has one.
    """
    for name in list(sys.modules):
        module = sys.modules.get(name)
        where = getattr(module, "__file__", None) or ""
        if where and Path(where).parent.name == "_engine":
            del sys.modules[name]


def _first(fields, *names):
    for name in names:
        value = fields.get(name)
        if value:
            return str(value).strip()
    return None


def open_crm(path=None):
    """The one call everything else makes. Raises NoCRM if there is nothing to open."""
    root = Path(path).expanduser() if path else find()
    if not root:
        raise NoCRM("no CRM found — point LinkChat at the folder you built in Session 1")
    bridge = Bridge(root)
    remember(bridge.root)
    return bridge


def main(argv):
    """python crm_bridge.py [path]  — say what LinkChat can see, and stop."""
    path = argv[1] if len(argv) > 1 else None
    try:
        bridge = open_crm(path)
    except NoCRM as exc:
        print("No CRM: %s" % exc)
        return 1
    state = bridge.state()
    print("CRM:      %s" % state["crm"])
    print("People:   %d" % len(bridge.people()))
    print("Ceiling:  %s used of %s" % (state["used"], state["cap"]))
    for job, ok in sorted(state["can"].items()):
        print("  %-6s %s" % (job, "yes" if ok else "no"))
    for name, why in sorted(state["missing"].items()):
        print("  missing: %s (%s)" % (name, why))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
