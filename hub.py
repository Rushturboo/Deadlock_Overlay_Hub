#!/usr/bin/env python3
"""Deadlock overlay hub.

One local process that reads the game through dl-live-bridge, folds the
snapshot into a single state object, and serves the overlay pages.

    py -3 hub.py                 # http://127.0.0.1:8787
    py -3 hub.py --port 9000

Why this exists: OBS browser sources are isolated processes with fixed urls, so
the overlay pages cannot talk to the game themselves. The hub holds the entity
-> player mapping, the widget state and the floating key strip in one place.

The only network calls left are the static art downloads (hero portraits, item
icons, the minimap plate) that are cached under data/ on first run.

Standard library only.
"""

import argparse
import ctypes
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# static art only (portraits, item icons, the map plate) - no live data
ART = "https://api.deadlock-api.com"
HERE = os.path.dirname(os.path.abspath(__file__))

HERO = {
    1: "Infernus", 2: "Seven", 3: "Vindicta", 4: "Lady Geist", 6: "Abrams",
    7: "Wraith", 8: "McGinnis", 10: "Paradox", 11: "Dynamo", 12: "Kelvin",
    13: "Haze", 14: "Holliday", 15: "Bebop", 16: "Calico", 17: "Grey Talon",
    18: "Mo & Krill", 19: "Shiv", 20: "Ivy", 21: "Kali", 25: "Warden",
    27: "Yamato", 31: "Lash", 35: "Viscous", 38: "Gunslinger", 39: "The Boss",
    48: "Wrecker", 49: "Rutger", 50: "Pocket", 51: "Thumper", 52: "Mirage",
    53: "Fathom", 54: "Cadence", 56: "Bomber", 57: "Shield Guy", 58: "Vyper",
    59: "Vandal", 60: "Sinclair", 61: "Trapper", 62: "Raven", 63: "Mina",
    64: "Drifter", 65: "Venator", 66: "Victor", 67: "Paige", 68: "Boho",
    69: "The Doorman", 70: "Skyrunner", 71: "Swan", 72: "Billy", 73: "Druid",
    74: "Graf", 75: "Fortuna", 76: "Graves", 77: "Apollo",
    78: "Deadman Danny", 79: "Rem", 80: "Silver", 81: "Celeste", 82: "Opera",
    84: "Rat King", 85: "Solomon", 86: "Violet", 87: "Nurse Harrow", 88: "Baba",
}

# the shop items you press on Z X C V. a cooldown in memory does NOT mean "active":
# passives run internal timers too (Active Reload, Headshot Booster, Capacitor's chain),
# so the ability row is filtered against this list by name -> id from /v1/assets/items.
# source: deadlockhandbook.com/items/active-items/ (checked against the Oct 2026 patch).
ACTIVE_ITEM_NAMES = frozenset((
    "Golden Goose Egg", "Grit", "Healing Rite", "Rusted Barrel",
    "Cold Front", "Fleetfoot", "Guardian Ward", "Restorative Locket", "Return Fire",
    "Slowing Hex", "Spirit Sap", "Split Shot",
    "Alchemical Fire", "Blood Tribute", "Cultist Sacrifice", "Decay", "Disarming Hex",
    "Dispel Magic", "Fury Trance", "Healing Nova", "Heroic Aura", "Knockdown",
    "Majestic Leap", "Metal Skin", "Rescue Beam", "Shadow Weave", "Silence Wave",
    "Warp Stone",
    "Arctic Blast", "Capacitor", "Colossus", "Cursed Relic", "Divine Barrier",
    "Echo Shard", "Ethereal Shift", "Focus Lens", "Infuser", "Magic Carpet",
    "Phantom Strike", "Refresher", "Scourge", "Unstoppable", "Vampiric Burst",
    "Vortex Web",
))

# NOTE: never put LIMIT in a live query. The engine treats it as a fixed budget:
# once that many rows are emitted the server sends "event: end" and closes the
# connection (measured: team LIMIT 6 -> 7 rows then end, same table without
# LIMIT streams for minutes). Dedupe happens here instead.


def now():
    return time.time()


def handle_index(h):
    """m_h* handles encode the entity index in the low 14 bits."""
    try:
        return int(h) % 16384
    except (TypeError, ValueError):
        return None


def _coord(pos, i, radius):
    """one world axis of a [x,y,z] position, or None when it is missing or absurd.

    dead players and spectators report empty arrays, and a spectator entity that
    never joined a team carries 3.4e38 sentinels.
    """
    if not pos or len(pos) <= i:
        return None
    v = pos[i]
    if not isinstance(v, (int, float)):
        return None
    v = float(v)
    if v != v or abs(v) > radius * 1.5:
        return None
    return round(v, 1)


def team_id_of(team):
    """Amber is 2 and Sapphire is 3 in the game's own team numbering."""
    if team is None:
        return None
    if isinstance(team, int):
        return team if team in (2, 3) else None
    text = str(team).strip().lower()
    if "amber" in text:
        return 2
    if "sapphire" in text:
        return 3
    return None


def _dist(a, b):
    """3d distance between two [x,y,z] positions; huge when either is missing."""
    if not a or not b or len(a) < 3 or len(b) < 3:
        return 1e18
    try:
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5
    except (TypeError, ValueError):
        return 1e18


def _cd_went_up(bits, held):
    """Did anything in a fresh cooldown read jump *above* the held one?

    A jump means a new cast started (the end timestamp moved forward), which is the
    one thing a frozen pause snapshot must still let through.
    """
    fresh = ((bits.get("ult") or {}).get("remain"),
             {i["id"]: i.get("remain") for i in bits.get("items") or []})
    old = ((held.get("ult") or {}).get("remain"),
           {i["id"]: i.get("remain") for i in held.get("items") or []})
    pairs = [(fresh[0], old[0])] + [(v, old[1].get(k)) for k, v in fresh[1].items()]
    for a, b in pairs:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a > b + 1.0:
            return True
    return False


def _wins_needed(bo):
    """Games a side has to win: bo1 -> 1, bo3 -> 2, bo5 -> 3."""
    return max(1, int(bo) // 2 + 1)


def _safe_logo(value):
    """A crest path is only kept when it really is a file inside the overlay folder.

    The panel is hand typed, so a stray `..` or a typo would otherwise either leak an
    absolute path into the widget or leave a broken image on air.
    """
    text = str(value or "").strip().replace("\\", "/").lstrip("/")
    if not text or len(text) > 120:
        return ""
    for ch in text:
        if not (ch.isalnum() or ch in "._/-"):
            return ""
    if ".." in text.split("/"):
        return ""
    full = os.path.join(HERE, text.replace("/", os.sep))
    return text if os.path.isfile(full) else ""


def apply_config(config, body):
    """Validate the hand written half of the config: names, series, crests."""
    if isinstance(body.get("title"), str):
        # the line above the clock: "GRAND FINAL", "ROUND 3", whatever the show calls it
        config["title"] = body["title"].strip()[:48]
    if isinstance(body.get("t0"), str):
        config["t0"] = body["t0"].strip()[:40] or "Hidden King"
    if isinstance(body.get("t1"), str):
        config["t1"] = body["t1"].strip()[:40] or "Arch Mother"
    if "bo" in body:
        try:
            bo = int(body["bo"])
        except (TypeError, ValueError):
            bo = None
        if bo in (1, 3, 5):
            config["bo"] = bo
    need = _wins_needed(config.get("bo") or 3)
    for key in ("w0", "w1"):
        if key in body:
            try:
                config[key] = int(body[key])
            except (TypeError, ValueError):
                pass
        # clamp after a bo change so lowering the series length trims old wins
        config[key] = max(0, min(need, int(config.get(key) or 0)))
    for key in ("logo0", "logo1"):
        if key in body:
            config[key] = _safe_logo(body[key])
    if "compact" in body:
        # players.html: long bars <-> squares with the ability panel
        config["compact"] = bool(body["compact"])
    if isinstance(body.get("names"), dict):
        # single upsert from the roster table: {"key": "Name"}. an empty string
        # clears the override, which falls back to the in-game name
        names = config.setdefault("names", {})
        for key, value in body["names"].items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            clean = " ".join(value.split())[:20]
            if clean:
                names[key[:64]] = clean
            else:
                names.pop(key[:64], None)
    if isinstance(body.get("hotkeys"), dict):
        # one entry per action; an empty string unbinds it. a combo we cannot
        # parse is dropped here so the panel can show what stuck
        hk = config.setdefault("hotkeys", {})
        for action, spec in body["hotkeys"].items():
            if not isinstance(action, str):
                continue
            if isinstance(spec, str) and parse_hotkey(spec):
                hk[action] = " ".join(spec.split())[:24]
            else:
                hk.pop(action, None)
    return config


def art_get(path, timeout=30):
    req = urllib.request.Request(ART + path, headers={
        "Accept": "application/json",
        "User-Agent": "deadlock-hub/2",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def portrait_path(hid):
    return os.path.join(HERE, "data", "portraits", "%d.png" % hid)


def fetch_portrait(hid):
    """download one hero portrait into the cache; True when the file is there after.

    serve_hero used to do this on first sight, which meant the very first frame of a
    new match showed an empty circle while the picture came down the wire. the hub
    now walks the whole hero table once at startup instead.
    """
    path = portrait_path(hid)
    if os.path.isfile(path):
        return True
    url = (HUB.store.hero_defs.get(hid) or {}).get("icon")
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "deadlock-hub/2"})
        with urllib.request.urlopen(req, timeout=20) as r:
            blob = r.read()
        if not blob.startswith(b"\x89PNG") and not blob.startswith(b"\xff\xd8"):
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
        return True
    except Exception:                                   # noqa: BLE001
        return False


def prefetch_portraits():
    """fill data/portraits for every hero the table knows, a few at a time."""
    for _ in range(60):
        if HUB.store.hero_defs:
            break
        time.sleep(0.5)
    ids = sorted(HUB.store.hero_defs)
    missing = [hid for hid in ids if not os.path.isfile(portrait_path(hid))]
    if not missing:
        print("portraits: all %d cached" % len(ids), flush=True)
        return
    got = 0
    for hid in missing:
        if fetch_portrait(hid):
            got += 1
    print("portraits: %d cached, %d fetched, %d still missing"
          % (len(ids), got, len(missing) - got), flush=True)


class Store:
    """Everything the widgets can see, guarded by one lock."""

    def __init__(self):
        self.lock = threading.RLock()
        self.phase = "idle"
        self.match_id = None
        self.error = None
        self.teams = {}
        self.ctrls = {}
        self.pawns = {}
        self.link = {}               # ctrl entity -> pawn entity, sticky per match
        self.proxy = None
        self.kills = []
        self.kill_ids = set()
        self.match_over = False
        self.config = {"t0": "Hidden King", "t1": "Arch Mother", "s0": "", "s1": "",
                       "delayMs": 0,
                       # the small line above the clock in the top bar; "" hides it
                       "title": "",
                       # series bookkeeping, filled in by hand on the panel: how many
                       # games the series is (bo1/bo3/bo5) and how many each side has won
                       "bo": 3, "w0": 0, "w1": 0,
                       # team crests, relative to the overlay folder (`logos/T1.png`)
                       "logo0": "", "logo1": "",
                       # players.html has two shapes: the long bars, and the squares
                       # with the ability panel. the panel flips this.
                       "compact": False,
                       # custom player names, keyed by steam id (the panel edits these).
                       # an empty/absent entry means "use the in-game name".
                       "names": {},
                       # global hotkeys: action -> key combo. these work while the
                       # game has focus, because the operator has to stay in game
                       "hotkeys": {"compact": "NumpadAdd"}}
        self.started_at = None
        self.last_row_at = None
        self.resolved_at = None
        self.mem_mode = False
        self.mem = None              # last snapshot from the memory bridge
        self.mem_rows = []           # players, already mapped to our shape
        self.mem_teams = {}
        self.mem_seen_at = None
        self.mem_error = None
        self.mem_by_pawn = {}        # pawn entity index -> mem row, for kill names
        self.mem_timers = {}         # urn / rift / midboss countdowns, straight from memory
        self.mid_hp = None           # last midboss health seen
        self.mid_hp_at = None        # when that number last moved
        self.mem_objectives = []     # only the neutral ones (midboss etc.)
        # towers (guardian/walker/high ground) per team: team id -> {"down", "total",
        # "known"}. "down" is always from the *other* team's point of view, i.e. what a
        # side has knocked over. shrines and the patron are counted separately and not
        # part of this, they are not "towers".
        self.struct = {}
        # did we ever read this teams towers in the current match?
        self.struct_seen = {2: False, 3: False}
        # 0 = both patron phases up, 1 = first phase down, 2 = patron dead
        self.patron_phase = {2: 0, 3: 0}
        self.patron_seen = {2: False, 3: False}
        self.patron_hp = {2: None, 3: None}
        self.stamp_val = 0.0         # cached mtime stamp of the widget files
        self.stamp_at = 0.0
        self.map_radius = 10752.0    # world half-width the minimap plate covers
        self.map_ready = False
        self.map_loaded = False
        self.map_objects = []        # [{kind, team, x, y}] straight from the snapshot
        self.map_midboss = None      # {x, y} when a midboss entity exists
        self.map_urn = None          # {x, y} while the soul urn is up
        self.kill_prev = {}          # slot -> (kills, deaths) from the previous poll
        self.kill_seq = 0
        self.items_loaded = False
        self.item_defs = {}          # item id -> {"name", "icon"} from /v1/assets/items
        self.active_items = set()    # the ids that are really button items (Z X C V)
        self.item_cd = {}            # id -> base cooldown from the shop data
        self.heroes_loaded = False
        self.hero_defs = {}          # hero id -> {"name", "icon"} from /v1/assets/heroes
        self.kills_mode = "heuristic"  # heuristic | off (set by the Hub)
        self.kills_note = None
        # cooldown lengths are not in memory, only the end timestamps. remember the
        # longest remaining time seen per key ("ult:13", "CCitadel_Item_MetalSkin")
        # and use that as the denominator for the fan.
        self.cd_total = {}
        self.ult_slot = {}
        # while the match is paused, hold each player's cooldowns where they were
        self.paused = False
        self.cd_hold = {}

        # apply_mem runs under this condition so the kill differ can wait on it
        self.cond = threading.Condition(self.lock)

    # --- output -------------------------------------------------------

    def reset_match(self):
        """drop everything that belongs to a match, keep the config and caches."""
        with self.lock:
            self.teams.clear()
            self.ctrls.clear()
            self.pawns.clear()
            self.link.clear()
            self.proxy = None
            self.kills = []
            self.kill_ids = set()
            self.match_id = None
            self.match_over = False
            self.error = None
            self.last_row_at = None
            self.mem = {}
            self.mem_rows = []
            self.mem_teams = {}
            self.mem_by_pawn = {}
            self.mem_timers = {}
            self.mem_objectives = []
            self.mem_error = None
            self.mem_seen_at = None
            self.map_objects = []
            self.map_midboss = None
            self.map_urn = None
            self.struct = {}
            self.struct_seen = {2: False, 3: False}
            self.patron_phase = {2: 0, 3: 0}
            self.patron_seen = {2: False, 3: False}
            self.patron_hp = {2: None, 3: None}
            self.mid_hp = None
            self.mid_hp_at = None
            self.kill_prev = {}
            self.kill_seq = 0
            self.cd_total = {}
            self.ult_slot = {}
            self.paused = False
            self.cd_hold = {}
            self.kills_note = None
            self.phase = "idle"
            self.clock_val = None
            self.started_at = now()
            with self.cond:
                self.cond.notify_all()

    def clock(self):
        """the match clock the last memory read carried, or None before a match."""
        with self.lock:
            return self.clock_val

    # --- memory bridge source ----------------------------------------

    def apply_mem(self, doc):
        with self.cond:
            self.mem_seen_at = now()
            self.mem_error = None
            if not doc.get("attached"):
                self.phase = "game_closed"
                self.mem = None
                self.mem_rows = []
                self.mem_teams = {}
                self.cond.notify_all()
                return
            snap = doc.get("snapshot")
            if not snap:
                self.phase = "menus"
                self.mem = None
                self.mem_rows = []
                self.mem_teams = {}
                self.cond.notify_all()
                return
            self.mem = snap
            game_state = str(snap.get("game_state") or "")
            context = str(snap.get("context") or "").lower()
            n_players = len(snap.get("players") or [])
            # context stays "Hideout" while you spectate: the client is not in a
            # hero slot, but the match data is right there. trust game_state
            # first, and only fall back to the hideout guess when nothing is
            # actually in progress.
            if game_state == "GameInProgress" and n_players >= 2:
                phase = "live"
            elif game_state in ("PostGame", "PostGame_PlayOfTheGame", "End", "Abandoned"):
                phase = "ended"
            elif game_state in ("PreGame", "Intro", "Loading", "PreGameWait"):
                phase = "pregame"
            elif "hideout" in context:
                phase = "hideout"
            else:
                phase = "pregame"
            self.phase = phase
            self.match_over = phase == "ended"
            self.last_row_at = now()
            new_match = snap.get("match_id")
            if new_match and new_match != self.match_id:
                # fresh match: the counters restart at zero, so a baseline from
                # the previous match would swallow the first kills
                self.kill_prev = {}
                self.struct_seen = {2: False, 3: False}
                self.patron_phase = {2: 0, 3: 0}
                self.patron_seen = {2: False, 3: False}
                self.patron_hp = {2: None, 3: None}
            if new_match:
                self.match_id = new_match
            winner = snap.get("winning_team")
            if winner in (2, 3):
                # the losing side's patron fell for good, so both phases are gone
                self.patron_phase[{2: 3, 3: 2}[winner]] = 2
            clock = snap.get("clock") or {}
            live_clock = clock.get("now")
            # `now` counts paused time as well. the in-game clock does not, so show
            # playing time: (elapsed - paused) holds still while the match is paused
            # even though `now` keeps climbing.
            start = clock.get("game_start_time")
            paused_ticks = clock.get("total_paused_ticks")
            tick_rate = clock.get("tick_rate") or 64.0
            if isinstance(live_clock, (int, float)) and isinstance(start, (int, float)):
                live_clock = float(live_clock) - float(start)
                if isinstance(paused_ticks, (int, float)) and tick_rate:
                    live_clock -= float(paused_ticks) / float(tick_rate)
                live_clock = max(0.0, live_clock)
            pause = snap.get("pause") or {}
            paused_now = bool(pause.get("game_paused") or pause.get("server_paused"))
            if paused_now != self.paused:
                self.cd_hold = {}
            self.paused = paused_now
            if isinstance(live_clock, (int, float)):
                self.clock_val = float(live_clock)
            self.mem_teams = {}
            for t in snap.get("teams") or []:
                tid = team_id_of(t.get("team"))
                if tid is None:
                    continue
                self.mem_teams[tid] = {
                    "id": tid,
                    "name": self.config["t0"] if tid == 2 else self.config["t1"],
                    "rawName": tid == 2 and "Amber" or "Sapphire",
                    "score": t.get("score") if isinstance(t.get("score"), int) else 0,
                    "souls": t.get("souls"),
                    "kills": t.get("kills") if isinstance(t.get("kills"), int) else None,
                    # the rejuv buff is a team thing: a team holds it after mid boss dies,
                    # and it comes straight off the snapshot as a per-team counter
                    "rejuv": t.get("rejuvenators") if isinstance(t.get("rejuvenators"), int) else 0,
                }
            self.teams = dict(self.mem_teams)
            rows = []
            names = (self.config.get("names") or {}) if isinstance(self.config.get("names"), dict) else {}
            for p in snap.get("players") or []:
                tid = team_id_of(p.get("team"))
                if tid is None or p.get("is_spectator"):
                    continue
                raw_name = p.get("name") or ("bot" if p.get("is_bot") else "?")
                # a stable handle for the custom-name table: the steam id survives
                # reconnects, the slot is only a fallback (bots have no steam id)
                pkey = str(p.get("steam_id") or "").strip()
                if not pkey:
                    pkey = "%d:%s" % (tid, p.get("slot") if p.get("slot") is not None else "?")
                custom = names.get(pkey) or ""
                rows.append({
                    "ctrl": p.get("slot") if p.get("slot") is not None else p.get("controller"),
                    # the bridge reports a 64 bit pawn pointer; the entity index is the
                    # low 14 bits, the same rule the entity tables use
                    "pawn": (p.get("pawn_handle") % 16384) if isinstance(p.get("pawn_handle"), int) else None,
                    "handle": p.get("pawn"),
                    "key": pkey,
                    "name": custom or raw_name,
                    "rawName": raw_name,
                    "team": tid,
                    "hero": p.get("hero_id"),
                    "heroName": HERO.get(p.get("hero_id"), "?"),
                    "level": p.get("level") or p.get("level_raw") or 1,
                    "gold": None,
                    "netWorth": p.get("net_worth") or 0,
                    "kills": p.get("kills") or 0,
                    "deaths": p.get("deaths") or 0,
                    "assists": p.get("assists") or 0,
                    "alive": p.get("is_alive"),
                    "hp": p.get("health"),
                    "hpMax": p.get("max_health"),
                    "verified": True,
                    "bot": bool(p.get("is_bot")),
                    "items": [i for i in (p.get("items") or []) if isinstance(i, int)],
                    "x": _coord(p.get("position"), 0, self.map_radius),
                    "y": _coord(p.get("position"), 1, self.map_radius),
                    "cd": self._cd_view(p, live_clock),
                })
            rows.sort(key=lambda r: (r["team"], r["ctrl"] if isinstance(r["ctrl"], int) else 0))
            self.mem_rows = rows
            self.mem_by_pawn = {r["pawn"]: r for r in rows if r.get("pawn") is not None}
            timers = snap.get("timers") or {}
            self.mem_timers = timers if isinstance(timers, dict) else {}
            self._read_map(snap)
            self._read_structures(snap)
            mstate = self.mem_timers.get("midboss_state")
            hp = mstate.get("health") if isinstance(mstate, dict) else None
            if hp is not None and hp != self.mid_hp:
                self.mid_hp = hp
                self.mid_hp_at = now()
            self._track_kills(rows, snap, emit=phase not in ("hideout", "menus", "game_closed", "bridge_down"))
            self.cond.notify_all()

    # --- kill attribution from memory --------------------------------
    #
    # the client exposes each player's own kill/death counters but never says
    # who killed whom (deadlock-reader/src/events.rs:26-33). so we diff the
    # counters between two polls: everyone whose kills went up is a killer,
    # everyone whose deaths went up is a victim, and the two lists get paired
    # inside the same window. one candidate is exact; several candidates fall
    # back to the closest position and are flagged `approx`; none means the
    # victim died to the map (creeps, tower, midboss) and is shown alone.

    def _track_kills(self, rows, snap, emit=True):
        # key by (team, slot): slots restart at 1 on each side, so the raw slot
        # alone would collide between Amber and Sapphire
        key = lambda r: (r["team"], r["ctrl"])   # noqa: E731
        prev = self.kill_prev
        self.kill_prev = {key(r): (r["kills"], r["deaths"]) for r in rows}
        if not prev or not emit:
            return
        killers, victims = [], []
        for r in rows:
            pk, pd = prev.get(key(r), (r["kills"], r["deaths"]))
            if r["kills"] < pk or r["deaths"] < pd:
                continue          # counters restarted, this row is just a baseline
            killers += [r] * max(0, r["kills"] - pk)
            victims += [r] * max(0, r["deaths"] - pd)
        if not victims:
            return

        # one player gaining kills inside the window is exact even when several
        # people died: nobody else could have done it
        spread = {}
        for k in killers:
            spread.setdefault(key(k), []).append(k)
        if len(spread) == 1:
            only = next(iter(spread.values()))
            for victim in victims:
                if victim["team"] != only[0]["team"]:
                    self._push_kill(only[0], victim, multi=len(victims))
            return

        # several killers in one window: the client never names the attacker, so
        # the deaths stand alone and one extra line says who came out ahead
        for victim in victims:
            self._push_kill(None, victim)
        if spread:
            top = sorted(spread.items(), key=lambda kv: len(kv[1]), reverse=True)
            if len(top) == 1 or len(top[0][1]) > len(top[1][1]):
                self._push_kill(None, None, summary=top[0][1][0], count=len(top[0][1]))

    def _push_kill(self, killer, victim, approx=False, multi=1, summary=None, count=0):
        self.kill_seq += 1
        if summary is not None:
            kind = "spree"
        elif killer is None:
            kind = "death"
        else:
            kind = "kill"
        self.kills.insert(0, {
            "id": "mem-%d" % self.kill_seq,
            "kind": kind,
            "multi": multi,
            "count": count,
            "attacker": killer["pawn"] if killer and killer.get("pawn") is not None else -1,
            "victim": victim["pawn"] if victim and victim.get("pawn") is not None else -1,
            "attackerName": killer["name"] if killer else (summary["name"] if summary else None),
            "victimName": victim["name"] if victim else None,
            "attackerTeam": killer["team"] if killer else (summary["team"] if summary else 0),
            "victimTeam": victim["team"] if victim else 0,
            "gold": None,
            "firstBlood": False,
            "approx": approx,
            "at": now(),
        })
        del self.kills[30:]

    def players(self):
        if self.mem_mode:
            return [dict(p) for p in self.mem_rows]
        out = []
        rev = None
        for e, c in self.ctrls.items():
            team = c.get("t")
            if team not in (2, 3):
                continue
            # a controller's m_hPawn can briefly point at garbage (death cam,
            # 0xFFFFFF sentinels) so a link that verified once is kept until the
            # match ends, and a new candidate only wins if it verifies too.
            cand = handle_index(c.get("pawn"))
            pawn = self.pawns.get(cand) if cand is not None else None
            if pawn is not None and handle_index(pawn.get("h")) == e:
                self.link[e] = cand
            elif e in self.link and self.link[e] in self.pawns:
                cand = self.link[e]
                pawn = self.pawns[cand]
            verified = pawn is not None and handle_index(pawn.get("h")) == e
            if not verified:
                # last resort: whoever claims this controller owns the link
                if rev is None:
                    rev = {}
                    for idx, p in self.pawns.items():
                        owner = handle_index(p.get("h"))
                        if owner is not None and owner not in rev:
                            rev[owner] = idx
                if e in rev:
                    cand = rev[e]
                    pawn = self.pawns.get(cand, pawn)
                    verified = pawn is not None and handle_index(pawn.get("h")) == e
            hp = pawn.get("hp") if pawn else None
            mx = pawn.get("mx") if pawn else None
            life = pawn.get("life") if pawn else None
            alive = c.get("alive")
            if alive is None:
                alive = life == 0
            out.append({
                "ctrl": e,
                "pawn": cand,
                "name": c.get("name") or "",
                "team": team,
                "hero": c.get("hero"),
                "heroName": HERO.get(c.get("hero"), "?"),
                "level": c.get("lvl") or 0,
                "gold": c.get("gold") or 0,
                "netWorth": c.get("nw") or 0,
                "kills": c.get("k") or 0,
                "deaths": c.get("d") or 0,
                "assists": c.get("a") or 0,
                "alive": bool(alive),
                "hp": hp or 0,
                "hpMax": mx or 0,
                "lifeState": life,
                "verified": verified,
            })
        out.sort(key=lambda p: (p["team"], p["ctrl"]))
        return out

    def stamp(self):
        """newest mtime of the widget files, so a browser can notice an edit.

        only the files an operator edits are looked at: everything under data/ changes
        whenever a cached icon lands and must NOT make the overlay reload itself.
        """
        if self.stamp_at and now() - self.stamp_at < 2.0:
            return self.stamp_val
        newest = 0.0
        try:
            for name in os.listdir(HERE):
                if name.endswith((".html", ".js", ".css")):
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(HERE, name)))
                    except OSError:
                        pass
        except OSError:
            pass
        self.stamp_val = round(newest, 3)
        self.stamp_at = now()
        return self.stamp_val

    def state(self):
        with self.lock:
            players = self.players()
            teams = []
            for tid in (2, 3):
                t = self.teams.get(tid) or {
                    "id": tid,
                    "name": self.config["t0"] if tid == 2 else self.config["t1"],
                    "score": 0,
                }
                teams.append({
                    "id": tid,
                    "name": self.config["t0"] if tid == 2 else self.config["t1"],
                    "rawName": t.get("rawName"),
                    "logo": self.config.get("logo0" if tid == 2 else "logo1") or "",
                    "score": t.get("score") or 0,
                    "netWorth": sum(p["netWorth"] for p in players if p["team"] == tid),
                    # kills are summed by the bridge, but fall back to the player rows
                    # when a snapshot left that column out
                    "kills": (
                        t["kills"] if t.get("kills") is not None
                        else sum(p["kills"] for p in players if p["team"] == tid)
                    ),
                    # null when the snapshot carries no object list
                    "structuresDown": (self.struct.get(tid) or {}).get("down"),
                    "structuresTotal": (self.struct.get(tid) or {}).get("total"),
                    "structuresKnown": (self.struct.get(tid) or {}).get("known", True),
                    # how many rejuv buffs this team is holding; None when unknown
                    "rejuv": t.get("rejuv"),
                })

            clock = self.clock()
            # the overlay runs `delayMs` behind the game, so the match clock
            # has to be shifted back by the same amount or it won't agree with
            # the video the audience is watching
            if clock is not None:
                clock = max(0.0, clock - max(0, self.config.get("delayMs") or 0) / 1000.0)
            phase = self.phase
            if self.mem_mode and phase in ("live", "menus", "stalled"):
                if not self.mem_seen_at or now() - self.mem_seen_at > 3:
                    phase = "bridge_down"
            if phase == "live" and self.last_row_at and now() - self.last_row_at > 20:
                phase = "stalled"
            if phase in ("live", "stalled") and not self.ctrls:
                phase = "live" if self.last_row_at else "resolving"

            return {
                "phase": phase,
                "matchId": self.match_id,
                "error": self.error,
                "clock": clock,
                "series": {
                    "bo": self.config.get("bo") or 3,
                    "a": self.config.get("w0") or 0,
                    "b": self.config.get("w1") or 0,
                },
                # the small line above the clock in the top bar
                "title": self.config.get("title") or "",
                # players.html swaps between long bars and the square + ability panel
                "compact": bool(self.config.get("compact")),
                # widgets reload themselves when this changes, so an edit lands in
                # OBS without anyone remembering to refresh the browser source
                "stamp": self.stamp(),
                "teams": teams,
                "players": players,
                "kills": [
                    {
                        "id": k["id"],
                        "kind": k.get("kind", "kill"),
                        "multi": k.get("multi", 1),
                        "count": k.get("count", 0),
                        "attacker": (
                            None if k.get("kind") == "death"
                            else (k.get("attackerName") or self.name_of(k["attacker"]))
                        ),
                        "attackerTeam": k.get("attackerTeam", self.team_of(k["attacker"])),
                        "victim": (
                            None if k.get("kind") == "spree"
                            else (k.get("victimName") or self.name_of(k["victim"]))
                        ),
                        "victimTeam": k.get("victimTeam", self.team_of(k["victim"])),
                        "gold": k["gold"],
                        "firstBlood": k["firstBlood"],
                        "approx": k.get("approx", False),
                        "ageMs": int((now() - k["at"]) * 1000),
                    }
                    for k in self.kills
                ],
                "timers": self.timers_view(),
                "map": self.map_view(),
                "itemDefs": self.item_defs_view(players),
                "streams": [
                    {
                        "name": "bridge",
                        "state": ("down" if self.phase == "bridge_down" else "open"),
                        "reconnects": 0,
                        "lastError": self.mem_error,
                        "silentFor": int(now() - self.mem_seen_at) if self.mem_seen_at else None,
                    }, {
                        "name": "kills (local)",
                        "state": "off" if self.kills_mode == "off" else "open",
                        "reconnects": 0,
                        "lastError": ("turned off" if self.kills_mode == "off"
                                      else "diffed from the kill counters; pairing is approximate"),
                        "silentFor": None,
                    },
                ],
                "delayMs": self.config.get("delayMs", 0),
                "updatedAt": now(),
            }

    def status(self):
        st = self.state()
        with self.lock:
            st["roster"] = [{
                "ctrl": p["ctrl"],
                "pawn": p["pawn"],
                "key": p.get("key"),
                "name": p["name"],
                "rawName": p.get("rawName") or p["name"],
                "hero": p["heroName"],
                "team": p["team"],
                "level": p["level"],
                "verified": p["verified"],
            } for p in self.players()]
            st["hotkeys"] = HUB.hotkeys_status()
            # "running" means a strip window exists, not "we own its child": a hub
            # restarted next to a still-open strip does not adopt it, and the
            # panel used to claim the strip was gone while it floated on screen
            alive = HUB.keys_proc is not None and HUB.keys_proc.poll() is None
            st["keys"] = {"running": bool(alive or HUB.strip_pids())}
            st["startedAt"] = self.started_at
            st["lastRowAt"] = self.last_row_at
            # the panel hydrates its inputs from here so two browsers agree
            st["config"] = {k: self.config.get(k) for k in
                            ("title", "t0", "t1", "logo0", "logo1", "bo", "w0", "w1",
                             "delayMs", "compact", "names", "hotkeys")}
        return st

    def name_of(self, pawn_idx):
        """kill feed gives pawn indexes; walk back to the controller row."""
        if self.mem_mode:
            row = self.mem_by_pawn.get(pawn_idx)
            return (row or {}).get("name") or "?"
        pawn = self.pawns.get(pawn_idx)
        if not pawn:
            return "?"
        ctrl_idx = handle_index(pawn.get("h"))
        ctrl = self.ctrls.get(ctrl_idx) or {}
        return ctrl.get("name") or "?"

    def team_of(self, pawn_idx):
        if self.mem_mode:
            row = self.mem_by_pawn.get(pawn_idx)
            return (row or {}).get("team") or 0
        pawn = self.pawns.get(pawn_idx)
        if not pawn:
            return 0
        return pawn.get("t") or 0

    def _read_map(self, snap):
        """objectives + midboss + urn, all in the same world space as the plate."""
        r = self.map_radius
        objs = []
        mid = None
        for o in snap.get("objectives") or []:
            if not isinstance(o, dict):
                continue
            kind = o.get("kind") or o.get("class")
            x = _coord(o.get("position"), 0, r)
            y = _coord(o.get("position"), 1, r)
            if x is None or y is None:
                continue
            if kind == "midboss":
                mid = {"x": x, "y": y}
                continue
            objs.append({"kind": kind, "team": team_id_of(o.get("team")), "x": x, "y": y})
        urn = None
        loc = (self.mem_timers or {}).get("urn_location")
        if isinstance(loc, list) and len(loc) >= 2:
            x = _coord(loc, 0, r)
            y = _coord(loc, 1, r)
            if x is not None and y is not None and (self.mem_timers.get("urn") or {}).get("present"):
                urn = {"x": x, "y": y}
        self.map_objects = objs
        self.map_midboss = mid
        self.map_urn = urn

    def map_view(self):
        return {
            "radius": self.map_radius,
            "objectives": list(self.map_objects),
            "midboss": self.map_midboss,
            "urn": self.map_urn,
        }

    # --- towers -------------------------------------------------------
    #
    # "how many of theirs did we knock over" comes straight from the objectives list:
    # guardian (C_NPC_TrooperBoss), walker (C_NPC_Boss_Tier2) and high ground
    # (C_NPC_BarrackBoss) are towers, shrines and the patron are not. Totals are counted
    # from this match instead of being hardcoded, so a different map layout shows the
    # right denominator instead of a silently wrong "3/12".
    TOWER_KINDS = ("guardian", "walker", "base_guardian", "shrine", "patron")
    # what a team starts with, per kind. destroyed structures vanish from the snapshot,
    # so this is the only way to count progress after attaching to a match in flight.
    # shrines count: 3 + 3 + 6 + 2 = 14 per side. the patron is worth two (it has two
    # phases), so the bar reads out of 16 and never falls back to a dash when only the
    # patron is left - its two phases only disappear from the count once it dies.
    TOWER_BASELINE = {"guardian": 3, "walker": 3, "base_guardian": 6, "shrine": 2, "patron": 2}

    def _read_structures(self, snap):
        """how many of the enemy's towers a side has knocked over.

        a destroyed structure does not stay in the entity list with state="destroyed"
        — it disappears from the snapshot. so "how many are gone" cannot be counted
        from what is left, it has to be measured against the number the mode starts
        with. that baseline is fixed per team (3 guardians + 3 walkers + 6 high ground
        towers = 12) and asking the snapshot for it is exactly what broke when the
        overlay attached to a match that was already half played.
        """
        standing = {2: 0, 3: 0}
        seen = {2: 0, 3: 0}
        known = True
        patron_present = {2: False, 3: False}
        patron_hp = {2: None, 3: None}
        for o in snap.get("objectives") or []:
            if not isinstance(o, dict):
                continue
            kind = str(o.get("kind") or "")
            if kind not in self.TOWER_KINDS:
                continue
            tid = team_id_of(o.get("team"))
            if tid not in standing:
                continue
            seen[tid] += 1
            state = str(o.get("state") or "").lower()
            if kind == "patron":
                # the patron is counted separately further down, because the client
                # gives it one entity for both of its phases
                patron_present[tid] = state != "destroyed"
                hp = o.get("health")
                patron_hp[tid] = hp if isinstance(hp, (int, float)) else None
            elif state != "destroyed":
                # still standing as far as we can tell. "unknown" counts as there:
                # dropping it would invent a kill, keeping it only hides one
                standing[tid] += 1
            if state not in ("standing", "destroyed"):
                known = False

        # the patron's first phase ending is only visible as "it was there, now it is
        # not (or at zero hp)". that bumps the side by one, so the bar reads 15 at the
        # phase switch instead of jumping straight from 14 to 16.
        for tid in (2, 3):
            if self.patron_phase.get(tid, 0) == 0 and self.patron_seen.get(tid):
                dead = (not patron_present[tid]) or (
                    patron_hp[tid] is not None and patron_hp[tid] <= 0)
                if dead:
                    self.patron_phase[tid] = 1
            self.patron_seen[tid] = patron_present[tid]
            self.patron_hp[tid] = patron_hp[tid]
            if seen[tid]:
                standing[tid] += max(0, 2 - self.patron_phase.get(tid, 0))
        total = sum(self.TOWER_BASELINE.values())
        other = {2: 3, 3: 2}
        struct = {}
        for tid in (2, 3):
            # a side knocked over whatever is missing from the *enemy* team
            theirs = other[tid]
            if seen[theirs]:
                self.struct_seen[theirs] = True
            if not seen[theirs]:
                # nothing left to read for that side. if we can still read *someone's*
                # structures (or we read theirs earlier this match), the only sane
                # reading is that theirs are all gone - report the full count instead
                # of a dash, which is what the bar showed when only the patron was left
                if seen[tid] or self.struct_seen.get(theirs):
                    struct[tid] = {"down": total, "total": total, "known": known}
                else:
                    struct[tid] = {"down": None, "total": None, "known": False}
                continue
            struct[tid] = {
                "down": max(0, min(total, total - standing[theirs])),
                "total": total,
                "known": known,
            }
        self.struct = struct

    def ensure_map(self):
        """cache /v1/assets/map plus the plate images, refreshed weekly."""
        if self.map_loaded:
            return
        self.map_loaded = True
        folder = os.path.join(HERE, "data", "map")
        meta = os.path.join(folder, "map.json")
        mid = os.path.join(folder, "mid.png")
        frame = os.path.join(folder, "frame.png")
        try:
            fresh = (os.path.isfile(meta) and os.path.isfile(mid)
                     and now() - os.path.getmtime(meta) < 7 * 24 * 3600)
            if not fresh:
                info = art_get("/v1/assets/map")
                os.makedirs(folder, exist_ok=True)
                with open(meta, "w", encoding="utf-8") as f:
                    json.dump(info, f)
                images = info.get("images") or {}
                for name, url, dest in (("mid", images.get("minimap"), mid),
                                        ("frame", images.get("frame"), frame)):
                    if not url:
                        continue
                    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-hub/2"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        blob = r.read()
                    if not blob.startswith(b"\x89PNG"):
                        print("map: %s was not a png" % name, flush=True)
                        continue
                    tmp = dest + ".tmp"
                    with open(tmp, "wb") as fh:
                        fh.write(blob)
                    os.replace(tmp, dest)
            else:
                with open(meta, "r", encoding="utf-8") as f:
                    info = json.load(f)
            radius = info.get("radius")
            if isinstance(radius, (int, float)) and radius > 0:
                self.map_radius = float(radius)
            self.map_ready = os.path.isfile(mid)
            print("map: radius %d, plate %s" % (self.map_radius, "ok" if self.map_ready else "missing"),
                  flush=True)
        except Exception as e:                       # noqa: BLE001
            self.map_loaded = False
            print("map: could not load (%s)" % e, flush=True)

    def timers_view(self):
        """urn / rift / midboss countdowns, already in seconds."""
        t = self.mem_timers or {}

        def block(key):
            raw = t.get(key)
            raw = raw if isinstance(raw, dict) else {}
            nxt = raw.get("next_spawn_in")
            return {
                "present": bool(raw.get("present")),
                "in": float(nxt) if isinstance(nxt, (int, float)) else None,
            }

        mid = block("midboss")
        state = t.get("midboss_state")
        if isinstance(state, dict):
            mid["hp"] = state.get("health")
            mid["hpMax"] = state.get("max_health")
        # the chip earns screen space only while the boss is actually losing health,
        # so it follows the hp moving instead of the spawn countdown
        mid["changing"] = bool(self.mid_hp_at and now() - self.mid_hp_at <= 15.0)
        return {"midboss": mid, "urn": block("urn"), "rift": block("rift")}

    def _cd_learn(self, key, remain):
        """remember the longest cooldown seen for this key, use it as the fan denominator.

        the game gives an end timestamp but never the length, so the first time a
        skill fires we only see a partial bar. joining a match mid-cooldown reads
        short and the fan looks almost full until the next cast corrects it.
        """
        if remain is None or remain <= 0:
            return None
        best = self.cd_total.get(key) or 0.0
        if remain > best:
            best = remain
            self.cd_total[key] = best
        return best

    @staticmethod
    def _cd_span(slot):
        """exact length of one cast, straight off the entity (end - start).

        better than learning "the longest we ever saw": a 90s ult and a 25s item
        both get their real length, so the wedge is proportional from second one.
        """
        start = (slot or {}).get("start")
        end = (slot or {}).get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            return None
        span = float(end) - float(start)
        if 0.5 <= span <= 900.0:
            return span
        return None

    def _cd_view(self, p, clock):
        """ultimate + the active items that carry a timer, in match seconds."""
        # keyed by name, not by (team, ctrl): while a match is paused the client can
        # blank those two fields, and every player would then share one cache entry
        pkey = (p.get("team"), p.get("name") or p.get("ctrl"))
        bits = {"ult": None, "items": []}
        hero = p.get("hero_id")
        trained = bool(p.get("ultimate_trained"))
        # which of the four ability slots is the ultimate varies by hero. the
        # snapshot publishes ultimate_cooldown_end but the entity is what actually
        # ticks, so learn the slot once by matching the two, then read the entity
        bridge_ult = None
        abil = (p.get("cooldowns") or {}).get("abilities") or []
        snap_end = p.get("ultimate_cooldown_end")
        hero_key = str(hero)
        if isinstance(snap_end, (int, float)) and snap_end > 0:
            for a in abil:
                if not isinstance(a, dict):
                    continue
                a_end = a.get("end")
                if isinstance(a_end, (int, float)) and abs(float(a_end) - float(snap_end)) < 1.0:
                    self.ult_slot[hero_key] = a.get("slot")
                    break
        want = self.ult_slot.get(hero_key)
        if want is None and len(abil) >= 4:
            want = 3
        for a in abil:
            if want is not None and isinstance(a, dict) and a.get("slot") == want:
                bridge_ult = a
                break
        remain = None
        if trained:
            bridged = (bridge_ult or {}).get("remain")
            if isinstance(bridged, (int, float)):
                remain = max(0.0, float(bridged))
            else:
                end = p.get("ultimate_cooldown_end")
                if isinstance(end, (int, float)) and isinstance(clock, (int, float)) and end > 0:
                    remain = max(0.0, float(end) - float(clock))
        key = "ult:%s" % hero
        total = self._cd_span(bridge_ult) or self._cd_learn(key, remain)
        bits["ult"] = {
            "trained": trained,
            "remain": None if remain is None else round(remain, 1),
            "total": None if total is None else round(total, 1),
            "ready": trained and (remain is None or remain <= 0.05),
        }

        # the row answers "which actives does this player carry", so the list comes
        # from the inventory (items[]) and the ability entities only supply timers.
        # relying on the entity list alone loses anything the game does not model
        # as an ability (Counterspell and friends never appear there at all).
        bridge_items = {}
        for slot in (p.get("cooldowns") or {}).get("items") or []:
            if not isinstance(slot, dict):
                continue
            item_id = slot.get("id")
            if isinstance(item_id, int) and item_id > 0:
                bridge_items[item_id] = slot
        worn = [i for i in (p.get("items") or []) if isinstance(i, int)]
        if self.active_items:
            wanted = [i for i in worn if i in self.active_items][:4]
        else:
            # the item table has not loaded yet: fall back to whatever carries a
            # timer, so the row is populated instead of empty
            wanted = list(bridge_items)[:4]
        for item_id in wanted:
            slot = bridge_items.get(item_id) or {}
            rem = slot.get("remain")
            rem = float(rem) if isinstance(rem, (int, float)) else None
            key = "item:%s" % (slot.get("class") or item_id)
            # three sources, best first: the entity stores the cooldown it actually
            # ran (so it already includes cooldown reduction), the shop table has
            # the base value, and the longest one we have seen is the last resort
            tot = (self._cd_span(slot)
                   or self.item_cd.get(item_id)
                   or self._cd_learn(key, rem))
            bits["items"].append({
                "key": slot.get("class") or str(item_id),
                "id": item_id,
                "remain": None if rem is None else round(rem, 1),
                "total": None if tot is None else round(tot, 1),
            })
        if self.paused:
            # a pause stops the match but not the clock we read, and the game does not
            # push cooldown ends forward while it is paused, so a raw read keeps
            # draining. hold what was on screen instead - but let a *new* cast through
            # (a jump upwards), otherwise a player who pops an item during a pause
            # would never see it.
            held = self.cd_hold.get(pkey)
            if held is not None and not _cd_went_up(bits, held):
                return held
            self.cd_hold[pkey] = bits
        return bits

    def item_defs_view(self, players):
        """only the item ids actually worn right now, so the payload stays small."""
        out = {}
        for p in players:
            for i in p.get("items") or []:
                if i in self.item_defs and str(i) not in out:
                    d = self.item_defs[i]
                    out[str(i)] = {"n": d.get("name"), "i": "/icon/%d.png" % i}
        return out

    def ensure_heroes(self):
        """hero id -> portrait url, same idea as the item table."""
        if self.heroes_loaded:
            return
        self.heroes_loaded = True
        path = os.path.join(HERE, "data", "heroes.json")
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    table = json.load(f)
            else:
                table = art_get("/v1/assets/heroes")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(table, f)
            for h in table or []:
                hid = h.get("id")
                if not isinstance(hid, int):
                    continue
                imgs = h.get("images") or {}
                self.hero_defs[hid] = {
                    "name": h.get("name"),
                    "icon": imgs.get("icon_image_small") or imgs.get("icon_hero_card"),
                }
            print("heroes: %d portraits" % len(self.hero_defs), flush=True)
        except Exception as e:                       # noqa: BLE001
            self.heroes_loaded = False
            print("heroes: could not load table (%s)" % e, flush=True)

    def ensure_items(self):
        """pull the id -> name/icon table once, and cache it next to the hub."""
        if self.items_loaded:
            return
        self.items_loaded = True
        path = os.path.join(HERE, "data", "items.json")
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    table = json.load(f)
            else:
                table = art_get("/v1/assets/items")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(table, f)
            for it in table or []:
                iid = it.get("id")
                if not isinstance(iid, int):
                    continue
                props = it.get("properties") or {}
                cd = props.get("AbilityCooldown")
                # the shop data mixes floats and numeric strings ("27"), so coerce
                base_cd = None
                if isinstance(cd, dict):
                    try:
                        base_cd = float(cd.get("value"))
                    except (TypeError, ValueError):
                        base_cd = None
                self.item_defs[iid] = {
                    "name": it.get("name"),
                    "icon": it.get("shop_image") or it.get("image") or it.get("image_webp"),
                    "type": it.get("type"),
                    "active": bool(it.get("is_active_item")) and bool(it.get("shopable")),
                    "cd": base_cd if isinstance(base_cd, (int, float)) else None,
                }
                if self.item_defs[iid]["cd"]:
                    self.item_cd[iid] = float(self.item_defs[iid]["cd"])
            # the shop data carries the flag itself, which beats any list we would
            # have to maintain by hand; the name list stays as a fallback for an
            # older cached table
            self.active_items = {iid for iid, d in self.item_defs.items() if d.get("active")}
            if not self.active_items:
                self.active_items = {
                    iid for iid, d in self.item_defs.items()
                    if d.get("name") in ACTIVE_ITEM_NAMES
                }
            print("items: %d definitions, %d active, %d with a base cooldown" %
                  (len(self.item_defs), len(self.active_items), len(self.item_cd)),
                  flush=True)
        except Exception as e:                       # noqa: BLE001
            self.items_loaded = False
            print("items: could not load table (%s)" % e, flush=True)


class MemFeed(threading.Thread):
    """Polls the local bridge (dl-live-bridge.exe) and feeds the store.

    The bridge reads the game's own memory, so there is no relay in the path:
    one read already *is* the current state and the only staleness is the poll
    interval.
    """

    def __init__(self, hub, url):
        super().__init__(daemon=True)
        self.hub = hub
        self.url = url
        self.stop_flag = threading.Event()
        self.fails = 0
        self.spawned_at = 0.0
        self.proc = None                # the bridge we started, so quit can kill it

    def stop(self):
        self.stop_flag.set()

    def ping(self, timeout=0.6):
        """is something already answering on the bridge port?"""
        try:
            with urllib.request.urlopen(self.url, timeout=timeout) as resp:
                resp.read(64)
            return True
        except Exception:                           # noqa: BLE001
            return False

    def _respawn(self):
        """bring the bridge back if it died: it is a plain exe next to us."""
        exe = os.path.join(HERE, "mem-bridge", "target", "release", "dl-live-bridge.exe")
        if not os.path.isfile(exe):
            return
        if time.time() - self.spawned_at < 30:
            return
        self.spawned_at = time.time()
        try:
            arg = str(int(self.url.rstrip("/").rsplit(":", 1)[1]))
            self.proc = subprocess.Popen(
                [exe, arg],
                cwd=os.path.dirname(exe),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,          # CREATE_NO_WINDOW
            )
            print("bridge: restarted %s on %s" % (os.path.basename(exe), arg), flush=True)
        except Exception as exc:                       # noqa: BLE001
            print("bridge: could not restart (%s)" % exc, flush=True)

    def run(self):
        while not self.stop_flag.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=4) as resp:
                    doc = json.loads(resp.read().decode("utf-8"))
                self.fails = 0
                self.hub.store.apply_mem(doc)
            except Exception as exc:                        # noqa: BLE001
                self.fails += 1
                with self.hub.store.lock:
                    self.hub.store.phase = "bridge_down"
                    self.hub.store.error = "bridge: %s" % exc
                    self.hub.store.mem_error = str(exc)
                if self.fails in (6, 60, 600):             # ~1s, 12s, 2min
                    self._respawn()
            self.stop_flag.wait(0.2)


# --- global hotkeys -------------------------------------------------
#
# the operator's focus has to stay on the game (OBS follows the foreground window
# for its window capture), so the panel cannot be the only way to flip a switch.
# RegisterHotKey is system wide and does not care which window has focus.

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

MOD_NAMES = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
             "shift": MOD_SHIFT, "win": MOD_WIN}

VK_NAMES = {
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
    "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
    "numpad8": 0x68, "numpad9": 0x69, "numpadadd": 0x6B, "numpadplus": 0x6B,
    "numpadsub": 0x6D, "numpadminus": 0x6D, "numpadmul": 0x6A,
    "numpaddiv": 0x6F, "numpaddot": 0x6E, "numpadenter": 0x0D,
    "add": 0x6B, "sub": 0x6D, "plus": 0x6B, "minus": 0x6D,
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
    "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}


def parse_hotkey(spec):
    """'Ctrl+Alt+1', 'NumpadAdd', 'F9' -> (modifiers, vk); None when unusable."""
    if not isinstance(spec, str) or not spec.strip():
        return None
    text = spec.strip().lower().replace(" ", "")
    mods = 0
    while "+" in text:
        head, rest = text.split("+", 1)
        if head in MOD_NAMES:
            mods |= MOD_NAMES[head]
            text = rest
        elif head in ("numpad", "num"):          # "numpad++" reads as numpad + plus
            text = "numpad+" + rest
            break
        else:
            break
    text = text.lstrip("+") or "+"
    vk = None
    if text in VK_NAMES:
        vk = VK_NAMES[text]
    elif text in ("+", "numpad+"):
        vk = 0x6B
    elif len(text) == 1 and text.isalnum():
        vk = ord(text.upper())
    elif text.startswith("f") and text[1:].isdigit() and 1 <= int(text[1:]) <= 12:
        vk = 0x70 + int(text[1:]) - 1
    if vk is None:
        return None
    return (mods | MOD_NOREPEAT, vk)


class Hotkeys(threading.Thread):
    """Owns the RegisterHotKey calls; hotkeys are thread affine, so the loop and
    the registration live on this thread and rebinds are queued to it."""

    def __init__(self, mapping, on_fire):
        super().__init__(daemon=True)
        self.mapping = dict(mapping or {})
        self.on_fire = on_fire
        self.queue = deque()
        self.stop_flag = threading.Event()
        self.errors = {}
        self.active = {}

    def rebind(self, mapping):
        self.queue.append(dict(mapping or {}))

    def stop(self):
        self.stop_flag.set()

    def _apply(self, user32, mapping):
        for hk_id in list(self.active):
            user32.UnregisterHotKey(None, hk_id)
        self.active = {}
        self.errors = {}
        for i, (action, spec) in enumerate(sorted(mapping.items()), start=1):
            parsed = parse_hotkey(spec)
            if parsed is None:
                if spec:
                    self.errors[action] = "cannot read %r" % spec
                continue
            mods, vk = parsed
            if not user32.RegisterHotKey(None, i, mods, vk):
                self.errors[action] = "%s is already taken" % spec
                continue
            self.active[i] = action

    def run(self):
        if sys.platform != "win32":
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        msg = wintypes.MSG()
        # any PeekMessage call creates this thread's message queue, which is what
        # RegisterHotKey posts WM_HOTKEY to
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._apply(user32, self.mapping)
        while not self.stop_flag.is_set():
            while self.queue:
                self._apply(user32, self.queue.popleft())
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY:
                    action = self.active.get(int(msg.wParam))
                    if action:
                        try:
                            self.on_fire(action)
                        except Exception:                    # noqa: BLE001
                            pass
            self.stop_flag.wait(0.02)
        for hk_id in list(self.active):
            user32.UnregisterHotKey(None, hk_id)
        self.active = {}


class Hub:
    def __init__(self, mem_url=None, use_kills=True, kills_mode="heuristic"):
        self.store = Store()
        self.stopped = threading.Event()
        self.feed = None
        self.use_kills = use_kills
        self.kills_mode = kills_mode      # heuristic | off
        self.store.kills_mode = kills_mode
        self.mem_url = mem_url
        self.store.mem_mode = bool(mem_url)
        self.hotkeys = None
        self.keys_proc = None          # the floating key strip, if it is running
        self.show_keys = True          # --no-keys turns it off
        self.port = 8787               # main() overwrites this with --port

    # --- hotkeys ------------------------------------------------------

    def start_hotkeys(self):
        if self.hotkeys is None:
            mapping = self.store.config.get("hotkeys") or {}
            self.hotkeys = Hotkeys(mapping, self.on_hotkey)
            self.hotkeys.start()

    def reload_hotkeys(self):
        if self.hotkeys is not None:
            self.hotkeys.rebind(self.store.config.get("hotkeys") or {})

    def hotkeys_status(self):
        if self.hotkeys is None:
            return {"active": {}, "errors": {}}
        return {
            "active": {a: s for a, s in (self.store.config.get("hotkeys") or {}).items()
                       if a in self.hotkeys.active.values()},
            "errors": dict(self.hotkeys.errors),
        }

    def on_hotkey(self, action):
        """what a registered key does. runs on the hotkey thread: keep it short."""
        if action == "compact":
            with self.store.lock:
                self.store.config["compact"] = not bool(self.store.config.get("compact"))

    # --- lifecycle ----------------------------------------------------

    def start_mem(self):
        self.start_hotkeys()
        threading.Thread(target=self.store.ensure_items, daemon=True).start()
        threading.Thread(target=self.store.ensure_heroes, daemon=True).start()
        threading.Thread(target=self.store.ensure_map, daemon=True).start()
        threading.Thread(target=prefetch_portraits, daemon=True).start()
        if self.feed is not None:
            self.feed.stop()
        with self.store.lock:
            self.store.reset_match()
            self.store.phase = "attaching"
            self.store.started_at = now()
        self.feed = MemFeed(self, self.mem_url)
        self.feed.start()
        # start-mem.cmd no longer opens a second console for the bridge: the hub
        # owns it, hidden, so quitting the hub can take it down cleanly
        if not self.feed.ping():
            self.feed._respawn()
        if self.show_keys:
            self.start_keys_window()

    def start(self):
        """the hub follows whatever the game is doing; there is no match to pick."""
        self.start_mem()

    def stop(self):
        if self.feed is not None:
            self.feed.stop()
            self.feed = None
        if self.hotkeys is not None:
            self.hotkeys.stop()
            self.hotkeys = None
        with self.store.lock:
            self.store.phase = "idle"

    # --- teardown -----------------------------------------------------

    def kill_bridge(self):
        """kill every dl-live-bridge.exe still running, however it was started.

        start-mem.cmd launches the exe in its own window *before* the hub exists,
        so the hub does not always own the process: killing by image name is the
        only way to guarantee nothing is left reading the game after quit.
        """
        killed = 0
        proc = getattr(self.feed, "proc", None) if self.feed is not None else None
        if proc is not None and proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=0x08000000)
                killed += 1
            except Exception:                       # noqa: BLE001
                pass
        try:
            done = subprocess.run(
                ["taskkill", "/IM", "dl-live-bridge.exe", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, creationflags=0x08000000,
            )
            if done.returncode == 0:
                killed += 1
        except Exception:                           # noqa: BLE001
            pass
        return killed

    def start_keys_window(self):
        """the floating key legend gets its own process: tk wants its own main loop,
        and if it ever falls over the hub keeps streaming."""
        if self.keys_proc is not None and self.keys_proc.poll() is None:
            return
        # the strip holds 127.0.0.1:8791 as its single-instance lock: if that is
        # taken, a strip is already on screen and spawning another is pointless
        try:
            with socket.create_connection(("127.0.0.1", 8791), timeout=0.4):
                print("keys: strip already running", flush=True)
                return
        except OSError:
            pass
        script = os.path.join(HERE, "keys_window.py")
        if not os.path.isfile(script):
            return
        try:
            self.keys_proc = subprocess.Popen(
                [sys.executable, script, "--hub", "http://127.0.0.1:%d" % self.port],
                cwd=HERE, creationflags=0x08000000)          # CREATE_NO_WINDOW
            print("keys: strip started", flush=True)
        except Exception as exc:                             # noqa: BLE001
            self.keys_proc = None
            print("keys: could not start the strip: %s" % exc, flush=True)

    def stop_keys_window(self):
        """close the strip, whoever started it.

        the strip is normally our own child, but a hub that crashed and was
        restarted can leave one behind: then keys_proc is None while a window is
        still floating over the game, and "stop everything" would lie. so also
        chase the pid file the strip writes, and the window title as a last
        resort - the window must always be killable from here.
        """
        proc, self.keys_proc = self.keys_proc, None
        killed = 0
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                killed += 1
            except Exception:                                    # noqa: BLE001
                pass
        for pid in self.strip_pids():
            if pid and pid != os.getpid():
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, creationflags=0x08000000)
                    killed += 1
                except Exception:                                # noqa: BLE001
                    pass
        if killed:
            print("keys: closed %d strip process(es)" % killed, flush=True)
        try:
            os.remove(os.path.join(HERE, "data", "keys.pid"))
        except Exception:                                        # noqa: BLE001
            pass

    def strip_pids(self):
        """pids of every live "deadlock keys" window.

        matched by window title rather than by a pid file: a pid file can go
        stale, and a recycled pid would point at somebody else's process.
        """
        pids = []
        try:
            user32 = ctypes.windll.user32
            enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                           ctypes.c_void_p)
            buf = ctypes.create_unicode_buffer(256)

            def visit(hwnd, _param):
                user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value == "deadlock keys":
                    pid = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    pids.append(int(pid.value))
                return True

            user32.EnumWindows(enum_proc(visit), None)
        except Exception:                                        # noqa: BLE001
            pass
        return sorted({p for p in pids if p})

    def shutdown(self):
        """one click: no hub, no bridge, no thread left behind."""
        print("hub: shutting down on request", flush=True)
        try:
            self.stop()
        except Exception as exc:                    # noqa: BLE001
            print("hub: stop raised %s" % exc, flush=True)
        self.stop_keys_window()
        n = self.kill_bridge()
        print("hub: killed %d bridge process(es) - bye" % n, flush=True)
        sys.stdout.flush()
        os._exit(0)

    def note_error(self, name, msg):
        with self.store.lock:
            self.store.error = "%s: %s" % (name, msg)


HUB = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def serve_stream(self):
        """push state to the widgets at up to 20Hz.

        polling every 400ms used to be half the reason the overlay felt behind
        the picture; this removes that latency, and the playout thread has
        already fixed the other half.
        """
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        deadline = now() + 3600
        try:
            while now() < deadline:
                time.sleep(1.0 / 20)
                body = json.dumps(HUB.store.state())
                self.wfile.write(("data: %s\n\n" % body).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def serve_icon(self, name):
        """item icons, cached on disk so OBS never hits the internet for them."""
        try:
            iid = int(os.path.splitext(os.path.basename(name))[0])
        except ValueError:
            return self._send(404, "not found", "text/plain")
        cache = os.path.join(HERE, "data", "icons")
        path = os.path.join(cache, "%d.png" % iid)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return self._send(200, fh.read(), "image/png")
        url = (HUB.store.item_defs.get(iid) or {}).get("icon")
        if not url:
            return self._send(404, "no icon", "text/plain")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "deadlock-hub/2"})
            with urllib.request.urlopen(req, timeout=20) as r:
                blob = r.read()
            os.makedirs(cache, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, path)
            return self._send(200, blob, "image/png")
        except Exception:                               # noqa: BLE001
            return self._send(502, "icon fetch failed", "text/plain")

    def serve_hero(self, name):
        """hero portraits, cached on disk just like the item icons."""
        try:
            hid = int(os.path.splitext(os.path.basename(name))[0])
        except ValueError:
            return self._send(404, "not found", "text/plain")
        path = portrait_path(hid)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return self._send(200, fh.read(), "image/png")
        if fetch_portrait(hid):
            with open(path, "rb") as fh:
                return self._send(200, fh.read(), "image/png")
        if not (HUB.store.hero_defs.get(hid) or {}).get("icon"):
            return self._send(404, "no portrait", "text/plain")
        return self._send(502, "portrait fetch failed", "text/plain")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            return self._json(HUB.store.state())
        if path == "/api/stream":
            return self.serve_stream()
        if path == "/api/status":
            return self._json(HUB.store.status())
        if path.startswith("/icon/"):
            return self.serve_icon(path[len("/icon/"):])
        if path.startswith("/hero/"):
            return self.serve_hero(path[len("/hero/"):])
        if path == "/":
            path = "/control.html"
        safe = os.path.normpath(path).lstrip("\\/")
        full = os.path.join(HERE, safe)
        if not full.startswith(HERE) or not os.path.isfile(full):
            return self._send(404, "not found", "text/plain")
        ext = os.path.splitext(full)[1].lower()
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".png": "image/png",
                 ".json": "application/json; charset=utf-8",
                 ".md": "text/plain; charset=utf-8"}.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if parsed.path == "/api/stop":
            HUB.stop()
            return self._json({"ok": True})
        if parsed.path == "/api/quit":
            # answer first, then die a moment later so the panel sees the ok
            threading.Timer(0.4, HUB.shutdown).start()
            return self._json({"ok": True, "bye": True})
        if parsed.path == "/api/keys":
            show = body.get("show", True)
            HUB.stop_keys_window()
            if show:
                HUB.start_keys_window()
            return self._json({"ok": True, "show": bool(show)})
        if parsed.path == "/api/config":
            with HUB.store.lock:
                apply_config(HUB.store.config, body)
                if "delayMs" in body:
                    try:
                        HUB.store.config["delayMs"] = max(-3000, min(5000, int(body["delayMs"])))
                    except (TypeError, ValueError):
                        pass
            if "hotkeys" in body:
                HUB.reload_hotkeys()
            return self._json({"ok": True, "config": HUB.store.config})
        return self._json({"error": "unknown endpoint"}, 404)


def main():
    global HUB
    ap = argparse.ArgumentParser(description="Deadlock overlay hub (local memory bridge)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--bridge", default="http://127.0.0.1:8788/",
                    help="url of dl-live-bridge, the process that reads the game")
    ap.add_argument("--no-kills", action="store_true",
                    help="turn the kill/died feed off")
    ap.add_argument("--no-keys", action="store_true",
                    help="do not open the floating spectate key strip")
    args = ap.parse_args()

    kills_mode = "off" if args.no_kills else "heuristic"
    HUB = Hub(mem_url=args.bridge, use_kills=kills_mode != "off", kills_mode=kills_mode)
    HUB.port = args.port
    HUB.show_keys = not args.no_keys
    # windows lets two listeners share a port when SO_REUSEADDR is set, so a
    # second hub would start happily and answer half the requests. probe first.
    busy = False
    try:
        with socket.create_connection(("127.0.0.1", args.port), timeout=1):
            busy = True
    except OSError:
        busy = False
    if busy:
        print("port %d is already taken by another hub - close it first:" % args.port, flush=True)
        print('  Get-NetTCPConnection -LocalPort %d -State Listen | '
              'ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }' % args.port, flush=True)
        return 1
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print("port %d is already taken (%s) - another hub is still running. close it first:"
              % (args.port, exc), flush=True)
        print('  Get-NetTCPConnection -LocalPort %d -State Listen | '
              'ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }' % args.port, flush=True)
        return 1
    srv.daemon_threads = True
    print("deadlock hub on http://127.0.0.1:%d" % args.port)
    print("source: memory bridge at %s (following whatever the game is doing)" % args.bridge)
    HUB.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
