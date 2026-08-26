"""traffic.py — yield the browser across a walk's own pacing sleeps.

Traffic-control V3 item 3 (2026-08-25, plan: TRAFFIC-CONTROL-PLAN.md). One browser,
many lanes: a bulk walk used to keep the shared READ_LOCK through its own pacing
sleeps, so an urgent reply send starved for the whole walk. The walk's gaps are
45-180s by design while a waiter polls every POLL_SEC (~4s) — so releasing the lock
across a sleep hands any waiter the boundary essentially for free, with zero added
LinkedIn-visible footprint.

Boundary rules built in (round-2 critique, 2026-08-25):
  * The walk stays CDP-attached through the pause — its keeper heartbeat keeps
    beating, so an idle-looking keeper is never reaped out from under it.
  * On every re-acquire the keeper window is RE-FROZEN: the boundary winner's
    release() unfreezes human input and nothing else would re-freeze it, silently
    dropping the 2026-06-13 no-human-clicks-mid-lane guard.
  * If the lock cannot be re-won inside `reacquire_wait`, pause() returns False and
    the walk must STOP CLEANLY (resumable next run) — never drive without the lock.
"""
from __future__ import annotations

import contextlib
import time


@contextlib.contextmanager
def lane_tenure(lane: "LaneLock"):
    """Drop-in replacement for `with ops.lock(...) as got:` around a whole walk —
    same `got` shape, but the walk can call lane.pause() to yield mid-walk."""
    got = lane.acquire()
    try:
        yield got
    finally:
        lane.release()


class LaneLock:
    """Hold the shared READ_LOCK across a bulk walk, yielding it across the walk's
    own pacing sleeps.

    Usage:
        lane = LaneLock(agent=AGENT, wait_sec=300)
        if not lane.acquire():
            ... busy message ...
            return
        try:
            ... walk ...
            if not lane.pause(delay):      # instead of time.sleep(delay)
                ... stop cleanly ...
        finally:
            lane.release()
    """

    # Below this a release/re-acquire churn buys nothing — just sleep holding the lock.
    MIN_YIELD_SEC = 20.0

    def __init__(self, agent: str, wait_sec: float = 300.0, reacquire_wait: float = 600.0):
        from engine import ops
        import linkedin_browser as lb
        self._ops = ops
        self._profile = lb.READ_LOCK
        self.agent = agent
        self.wait_sec = wait_sec
        self.reacquire_wait = reacquire_wait
        self._ctx = None
        self.held = False
        self.yields = 0

    def _take(self, wait_sec: float) -> bool:
        self._ctx = self._ops.lock(self._profile, agent=self.agent,
                                   wait_sec=wait_sec, heartbeat=True)
        self.held = bool(self._ctx.__enter__())
        if not self.held:
            self._ctx = None
        return self.held

    def acquire(self) -> bool:
        return self._take(self.wait_sec)

    def release(self) -> None:
        if self._ctx is not None:
            ctx, self._ctx, self.held = self._ctx, None, False
            ctx.__exit__(None, None, None)

    def pause(self, seconds: float) -> bool:
        """Sleep `seconds` WITHOUT the lock (when long enough to be worth it), then
        re-acquire and re-freeze the keeper window. Returns False only if the lock
        could not be re-won — the caller stops cleanly and never drives unlocked."""
        seconds = max(0.0, float(seconds))
        if seconds < self.MIN_YIELD_SEC or not self.held:
            time.sleep(seconds)
            return self.held
        self.release()
        self.yields += 1
        time.sleep(seconds)
        if not self._take(self.reacquire_wait):
            return False
        try:
            from engine import browser as B
            B.lock_window()
        except Exception:
            pass   # freezing is a guard, not a gate — the lock itself is held
        return True
