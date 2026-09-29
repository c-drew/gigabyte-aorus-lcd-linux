"""Screensaver rotation: one ttfx effect per loop, the next loop written to the
panel's hidden GIF bank and then switched in. Playback is held while it is
written (the screen keeps its last frame for a minute or two): written while
an animation plays, the flash erase runs at a speed that depends on that
animation, too unpredictable to time (see protocol.py).

Every switch erases a bank of the panel's GIF flash, which is rated for about
100,000 erases per sector (typical SPI NOR). With two banks taking turns,
rotating every N minutes lasts 100,000 x 2N minutes: every 15 minutes is ~6
years, hourly ~23. Hence the 15-minute floor, and the wear count kept here.
"""
import random
import time

from . import protocol as P

MIN_ROTATE_MINUTES = 15
RATED_ERASES = 100_000


def effects_in_order(c, default):
    """The playlist order: the configured effects (or `default`), shuffled per seed if asked."""
    order = list(c.effects or default)
    if c.shuffle:
        random.Random(c.seed).shuffle(order)
    return order


class Rotation:
    """Where the playlist is and how worn the flash is, kept in the service's
    state (a dict saved to state.json by the caller)."""

    def __init__(self, state):
        self.state = state.setdefault("rotation", {})
        self.state.setdefault("index", 0)
        self.state.setdefault("switched_at", 0.0)
        wear = state.setdefault("wear", {})
        wear.setdefault("erases", {"1": 0, "2": 0})
        wear.setdefault("uploads", 0)
        self.wear = wear

    def effect(self, order, index=None):
        return order[(self.state["index"] if index is None else index) % len(order)]

    def next_index(self, order):
        return (self.state["index"] + 1) % len(order)

    def due_at(self, c):
        return self.state["switched_at"] + max(MIN_ROTATE_MINUTES, c.rotate_minutes) * 60

    def switched(self, index, effect, now=None):
        self.state.update(index=index, effect=effect, switched_at=now or time.time())

    def erased(self, bank, times=1):
        """Count an upload into `bank` (1 or 2), which erased it `times` times."""
        key = str(bank)
        self.wear["erases"][key] = self.wear["erases"].get(key, 0) + times
        self.wear["uploads"] += 1

    def report(self, c=None):
        worst = max(self.wear["erases"].values() or [0])
        out = {"uploads": self.wear["uploads"], "erases": dict(self.wear["erases"]),
               "used_percent": round(100 * worst / RATED_ERASES, 3)}
        if c is not None and c.rotate_minutes:
            minutes = max(MIN_ROTATE_MINUTES, c.rotate_minutes)
            per_year = 365 * 24 * 60 / (2 * minutes)       # erases per bank per year
            out["years_left"] = round((RATED_ERASES - worst) / per_year, 1)
        return out


def estimate_seconds(upload):
    """How long writing `upload` takes (playback held: 1.5 MB took 60 s)."""
    n = P.chunk_count(len(upload.payload))
    return P.header_pause(len(upload.payload), upload.erase_mode) + n * 0.0075
