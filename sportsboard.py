#!/usr/bin/env python3
"""
Sportsboard - a spoiler-safe scoreboard server for the Jetson Nano.

Polls ESPN's public scoreboard feeds, keeps every snapshot in a rolling
buffer, and only ever serves data that is at least `delay_seconds` old.
Nothing newer than the delay ever leaves this process, so the browser
cannot leak a score ahead of your TV.

Standard library only. Tested against Python 3.6 (JetPack 4.x).
"""

import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = "https://site.api.espn.com/apis/site/v2/sports"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------


def load_config():
    path = os.path.join(HERE, "config.json")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("port", 8080)
    cfg.setdefault("delay_seconds", 180)
    cfg.setdefault("poll_seconds", 20)
    cfg.setdefault("cold_poll_seconds", 300)
    cfg.setdefault("days_ahead", 8)
    cfg.setdefault("user_agent", DEFAULT_UA)
    # SPORTSBOARD_UA lets you work around a picky network without editing config
    cfg["user_agent"] = os.environ.get("SPORTSBOARD_UA", cfg["user_agent"])
    cfg["feeds"] = [f for f in cfg.get("feeds", []) if f.get("enabled", True)]
    return cfg


CFG = load_config()

# ----------------------------------------------------------------------------
# fetching + normalising
# ----------------------------------------------------------------------------


def date_range():
    """Local calendar window: yesterday through days_ahead, as ESPN wants it."""
    now = datetime.now()
    start = (now - timedelta(days=1)).strftime("%Y%m%d")
    end = (now + timedelta(days=CFG["days_ahead"])).strftime("%Y%m%d")
    return start + "-" + end


def fetch(feed):
    url = "{}/{}/scoreboard?dates={}".format(BASE, feed["path"], date_range())
    if feed.get("query"):
        url += "&" + feed["query"]
    req = urllib.request.Request(url, headers={"User-Agent": CFG["user_agent"]})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_iso(value):
    """ESPN returns 2026-09-05T16:00Z or ...T16:00:00Z. Return epoch seconds."""
    if not value:
        return None
    txt = value.replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            naive = datetime.strptime(txt, fmt)
            return naive.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def side(competitor):
    team = competitor.get("team") or {}
    logos = team.get("logos") or []
    logo = team.get("logo") or (logos[0].get("href") if logos else None)
    color = team.get("color")
    rank = (competitor.get("curatedRank") or {}).get("current")
    records = competitor.get("records") or []
    record = records[0].get("summary") if records else None
    full = team.get("displayName") or team.get("name") or ""
    short = team.get("shortDisplayName") or full
    return {
        "name": (full if 0 < len(full) <= 21 else (short or "TBD")),
        "full": team.get("displayName") or "",
        "abbr": team.get("abbreviation") or "",
        "logo": logo,
        "color": ("#" + color) if color and color.lower() not in ("ffffff", "none") else None,
        "score": competitor.get("score"),
        "shootout": competitor.get("shootoutScore"),
        "rank": rank if isinstance(rank, int) and 0 < rank <= 25 else None,
        "record": record,
        "winner": competitor.get("winner"),
    }


def money(node):
    """One side of a moneyline. Prefer the closing number, fall back to open."""
    if not isinstance(node, dict):
        return None
    for phase in ("close", "current", "open"):
        block = node.get(phase)
        if isinstance(block, dict):
            value = block.get("odds")
            if value and value != "OFF":
                return value
    return None


def odds_of(comp):
    """DraftKings line for a fixture, or None.

    Soccer feeds hand back `odds: [null]` rather than an empty list, so the
    entries have to be filtered for dicts before anything is read off them.
    """
    entries = [o for o in (comp.get("odds") or []) if isinstance(o, dict)]
    if not entries:
        return None
    o = entries[0]

    home_side = o.get("homeTeamOdds") or {}
    away_side = o.get("awayTeamOdds") or {}
    favourite = None
    for candidate in (home_side, away_side):
        if candidate.get("favorite"):
            favourite = (candidate.get("team") or {}).get("abbreviation")
            break

    ml = o.get("moneyline") or {}
    line = {
        "book": ((o.get("provider") or {}).get("displayName")
                 or (o.get("provider") or {}).get("name") or ""),
        "details": o.get("details") or "",
        "overUnder": o.get("overUnder"),
        "favorite": favourite,
        "homeML": money(ml.get("home")),
        "awayML": money(ml.get("away")),
    }
    if not (line["details"] or line["overUnder"] or line["homeML"] or line["awayML"]):
        return None
    return line


def matches(feed, home, away):
    keys = feed.get("teams", "*")
    if keys == "*":
        return True

    fields = [home["full"], home["name"], home["abbr"],
              away["full"], away["name"], away["abbr"]]
    hay = " | ".join(fields).lower()
    exact = set(f.strip().lower() for f in fields if f)

    for k in keys:
        k = k.lower()
        # "=alabama" means the whole team name, not a substring. Needed where
        # one school's name sits inside another's -- plain "alabama" would
        # otherwise drag in Alabama State and South Alabama as well.
        if k.startswith("="):
            if k[1:].strip() in exact:
                return True
        elif k in hay:
            return True
    return False


def normalise(feed, payload):
    league_name = ((payload.get("leagues") or [{}])[0]).get("name") or feed["key"]
    games = []
    for ev in payload.get("events") or []:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        entries = comp.get("competitors") or []
        if len(entries) < 2:
            continue

        home = away = None
        for c in entries:
            if c.get("homeAway") == "away":
                away = side(c)
            else:
                home = side(c)
        if not home or not away:
            home, away = side(entries[0]), side(entries[1])

        if not matches(feed, home, away):
            continue

        status = ev.get("status") or comp.get("status") or {}
        stype = status.get("type") or {}
        state = stype.get("state") or "pre"

        broadcast = ""
        for b in comp.get("broadcasts") or []:
            names = b.get("names") or []
            if names:
                broadcast = "/".join(names)
                break

        venue = (comp.get("venue") or {}).get("fullName") or ""

        games.append({
            "id": str(ev.get("id")),
            "feed": feed["key"],
            "league": feed.get("label") or league_name,
            "state": state,
            "start": parse_iso(ev.get("date")),
            "detail": stype.get("shortDetail") or stype.get("detail") or "",
            "clock": status.get("displayClock"),
            "period": status.get("period"),
            "home": home,
            "away": away,
            "broadcast": broadcast,
            "venue": venue,
            "odds": odds_of(comp),
            "note": (comp.get("notes") or [{}])[0].get("headline", "") if comp.get("notes") else "",
        })
    return games


# ----------------------------------------------------------------------------
# poller
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# Alabama football
#
# The scoreboard feeds only reach days_ahead into the future, so out of season
# - or during a bye - the next Bama game falls outside the window entirely and
# simply isn't in the data. This pulls the team's own schedule so the marquee
# slide always has a game to show, however far off it is.
# ----------------------------------------------------------------------------

BAMA_TEAM_ID = "333"        # Alabama Crimson Tide, ESPN's college-football id
BAMA_URL = ("https://site.api.espn.com/apis/site/v2/sports/football/"
            "college-football/teams/{}/schedule".format(BAMA_TEAM_ID))


def fetch_bama_next():
    """The next scheduled (or in-progress) Alabama football game, or None."""
    req = urllib.request.Request(BAMA_URL, headers={"User-Agent": CFG["user_agent"]})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    now = time.time()
    best = None
    for ev in payload.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        start = parse_iso(ev.get("date"))
        state = ((comp.get("status") or {}).get("type") or {}).get("state") or "pre"
        # keep anything still to come, plus anything currently running
        if state == "in" or (start and start >= now - 4 * 3600):
            if best is None or (start or 0) < (best[0] or 0):
                best = (start, ev, comp, state)

    if not best:
        return None
    start, ev, comp, state = best

    entries = comp.get("competitors") or []
    home = away = None
    for c in entries:
        if c.get("homeAway") == "away":
            away = side(c)
        else:
            home = side(c)
    if not home or not away:
        if len(entries) < 2:
            return None
        home, away = side(entries[0]), side(entries[1])

    broadcast = ""
    for b in (comp.get("broadcasts") or []):
        names = b.get("names") or []
        if names:
            broadcast = names[0]
            break

    return {
        "id": "bama-next-" + str(ev.get("id") or ""),
        "league": "ALABAMA FOOTBALL",
        "start": start,
        "state": state,
        "home": home,
        "away": away,
        "venue": (((comp.get("venue") or {}).get("fullName")) or ""),
        "broadcast": broadcast,
        "odds": odds_of(comp),
        "detail": ((comp.get("status") or {}).get("type") or {}).get("shortDetail") or "",
    }


class Board(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}          # feed key -> list of games (last good result)
        self.errors = {}         # feed key -> error string
        self.due = {}            # feed key -> next monotonic time to poll
        self.buffer = deque()    # (wall_clock, snapshot list)
        self.started = time.time()
        self.last_success = None
        self.bama = None         # next Alabama football game
        self.bama_due = 0

    def hot(self, key):
        """A feed is hot if it has a game today or in progress."""
        today = time.strftime("%Y-%m-%d")
        for g in self.cache.get(key, []):
            if g["state"] == "in":
                return True
            if g["start"] and time.strftime("%Y-%m-%d", time.localtime(g["start"])) == today:
                return True
        return False

    def poll_one(self, feed):
        key = feed["key"]
        try:
            games = normalise(feed, fetch(feed))
            with self.lock:
                self.cache[key] = games
                self.errors.pop(key, None)
                self.last_success = time.time()
        except Exception as exc:  # network hiccup: keep the last good result
            with self.lock:
                self.errors[key] = "{}: {}".format(type(exc).__name__, exc)

    def cycle(self, pool):
        now = time.monotonic()
        due_now = []
        for feed in CFG["feeds"]:
            key = feed["key"]
            if now >= self.due.get(key, 0):
                due_now.append(feed)
        if due_now:
            list(pool.map(self.poll_one, due_now))
        after = time.monotonic()
        for feed in due_now:
            key = feed["key"]
            gap = CFG["poll_seconds"] if self.hot(key) else CFG["cold_poll_seconds"]
            self.due[key] = after + gap

        # Alabama's own schedule, refreshed rarely - it only changes between
        # games, and a failure here must never take the rest of the board down.
        if time.monotonic() >= self.bama_due:
            try:
                nxt = fetch_bama_next()
                with self.lock:
                    self.bama = nxt
            except Exception as exc:
                sys.stderr.write("bama schedule fetch failed: {}\n".format(exc))
            self.bama_due = time.monotonic() + 600

        with self.lock:
            snapshot = []
            for games in self.cache.values():
                snapshot.extend(games)
            snapshot.sort(key=lambda g: (g["start"] or 0))
            self.buffer.append((time.time(), snapshot))
            cutoff = time.time() - (CFG["delay_seconds"] + 120)
            while len(self.buffer) > 2 and self.buffer[0][0] < cutoff:
                self.buffer.popleft()

    def run(self):
        pool = ThreadPoolExecutor(max_workers=8)
        while True:
            try:
                self.cycle(pool)
            except Exception as exc:
                sys.stderr.write("poll cycle failed: {}\n".format(exc))
            time.sleep(5)

    def delayed(self):
        """Newest snapshot that is at least delay_seconds old."""
        target = time.time() - CFG["delay_seconds"]
        with self.lock:
            chosen = None
            for ts, snap in self.buffer:
                if ts <= target:
                    chosen = (ts, snap)
                else:
                    break
            if chosen:
                return chosen[0], chosen[1], False
            if self.buffer:
                ts, snap = self.buffer[0]
                return ts, snap, True   # still warming: redact
            return None, [], True


BOARD = Board()


def redact(game):
    """Hide anything that could spoil while the buffer is still filling."""
    g = json.loads(json.dumps(game))
    if g["state"] in ("in", "post"):
        g["home"]["score"] = None
        g["away"]["score"] = None
        g["home"]["winner"] = None
        g["away"]["winner"] = None
        g["clock"] = None
        g["period"] = None
        g["detail"] = "HIDDEN"
        # a live line moves with the scoreline, so it spoils just as hard
        g["odds"] = None
        g["redacted"] = True
    return g


def build_response():
    ts, snapshot, warming = BOARD.delayed()
    if warming:
        snapshot = [redact(g) for g in snapshot]

    today = time.strftime("%Y-%m-%d")
    for g in snapshot:
        g["day"] = time.strftime("%Y-%m-%d", time.localtime(g["start"])) if g["start"] else ""
        g["today"] = (g["day"] == today)

    # The marquee Alabama football game. Prefer the copy inside the delayed
    # snapshot when the game is close enough to be in the normal feeds, since
    # that one has already been through the spoiler delay. Only fall back to
    # the schedule fetch when it isn't there - and that is always a future
    # fixture with no score to give away.
    bama = None
    for g in snapshot:
        names = (g["home"].get("full", "") + " " + g["away"].get("full", "")).lower()
        if "crimson tide" in names:
            if bama is None or (g["start"] or 0) < (bama["start"] or 0):
                bama = g
    if bama is None:
        with BOARD.lock:
            bama = json.loads(json.dumps(BOARD.bama)) if BOARD.bama else None
        if bama and warming:
            bama = redact(bama)
    if bama:
        bama["day"] = time.strftime("%Y-%m-%d", time.localtime(bama["start"])) if bama["start"] else ""
        bama["today"] = (bama["day"] == today)

    elapsed = time.time() - BOARD.started
    return {
        "bama": bama,
        "serverTime": time.time(),
        "snapshotTime": ts,
        "delaySeconds": CFG["delay_seconds"],
        "warming": warming,
        "warmingRemaining": max(0, int(CFG["delay_seconds"] - elapsed)) if warming else 0,
        "lastSuccess": BOARD.last_success,
        "errorCount": len(BOARD.errors),
        "errors": sorted(BOARD.errors.keys()),
        "games": snapshot,
    }


# ----------------------------------------------------------------------------
# http
# ----------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the console clean on a headless box

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/board":
            self._send(200, json.dumps(build_response()), "application/json")
            return
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except IOError:
                self._send(500, "index.html is missing next to sportsboard.py", "text/plain")
            return
        self._send(404, "not found", "text/plain")


class ThreadedServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    thread = threading.Thread(target=BOARD.run)
    thread.daemon = True
    thread.start()

    port = CFG["port"]
    print("Sportsboard running on http://0.0.0.0:{}".format(port))
    print("Delay: {}s across {} feeds".format(CFG["delay_seconds"], len(CFG["feeds"])))
    ThreadedServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
