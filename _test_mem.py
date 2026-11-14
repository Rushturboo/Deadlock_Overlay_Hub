"""Offline check for hub.py --source mem: fake bridge, real hub, one assert per field.

Run with:  py -3 _test_mem.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEROES = [7, 13, 15, 11, 4, 2, 6, 8, 10, 12, 19, 31]


SAMPLES = [668299740, 7409189, 2829638276, 2951612397, 2356412290]


# One team's real structure inventory: 3 guardians, 3 walkers, 6 high ground,
# 2 shrines and the patron. The patron is two phases in game but only the first is a
# separate objective here.
TEAM_STRUCTURES = [
    ("guardian", "C_NPC_TrooperBoss", 3),
    ("walker", "C_NPC_Boss_Tier2", 3),
    ("base_guardian", "C_NPC_BarrackBoss", 6),
    ("shrine", "C_Citadel_Destroyable_Building", 2),
    ("patron", "C_NPC_Boss_Tier3", 1),
]

# Which of those are already down, per team, so the tower counter has something to say.
# Amber knocked over 2 guardians + 1 walker (3 towers), Sapphire 1 walker (1 tower).
DESTROYED = {
    "Amber": [("guardian", 0), ("guardian", 1), ("walker", 0)],
    "Sapphire": [("walker", 0)],
}


def objectives():
    """Every structure on the map, spread around the plate so the map stays readable."""
    out = []
    for t, team in enumerate(("Amber", "Sapphire")):
        down = DESTROYED.get(team) or []
        radius = 9000 if t == 0 else 7600
        for kind, cls, count in TEAM_STRUCTURES:
            for i in range(count):
                out.append({
                    "class": cls,
                    "kind": kind,
                    "team": team,
                    "position": [
                        round(radius * (0.35 + 0.5 * (i / max(1, count))) * (1 if t else -1), 1),
                        round(radius * 0.6 * (1 if t == 0 else -1) + 900 * i, 1),
                        120.0,
                    ],
                    "state": "destroyed" if (kind, i) in down else "standing",
                })
    out.append({"class": "C_NPC_MidBoss", "kind": "midboss", "team": "NEUTRAL",
                "position": [-240.0, 395.0, 480.0], "state": "standing"})
    return out


def fixture():
    players = []
    for i in range(12):
        team = "Amber" if i < 6 else "Sapphire"
        players.append({
            "controller": 1000 + i,
            "slot": (i % 6) + 1,
            "team": team,
            "hero_id": HEROES[i],
            "name": "player%d" % i,
            "is_bot": False,
            "is_spectator": False,
            "level": 10 + i,
            "net_worth": 5000 + 1000 * i,
            "kills": i % 5,
            "deaths": (i + 1) % 4,
            "assists": i % 3,
            "health": 900,
            "max_health": 1200,
            "is_alive": i % 3 != 0,
            "pawn": 2000 + i,
            # p1 carries the three items the cooldown block below describes,
            # because the ability row now reads the inventory, not the timers
            "items": ([1976391348, 3270001687, 381961617] if i == 1
                      else SAMPLES[: (i % 6)]),
            "ultimate_trained": True,
            "ultimate_cooldown_end": 754.5 + (90.0 if i == 1 else 0.0),
            # one active item with a running timer, one active that is ready, and
            # one passive whose internal timer must NOT reach the ability row.
            # "id" is what the bridge reads off m_nSubclassID; pairing by
            # inventory position is wrong, so the test pins the id.
            "cooldowns": {"items": [
                {"slot": 12, "idx": 1, "id": 1976391348, "class": "CCitadel_Item_ColdFront",
                 "start": 80.0, "end": 104.0,
                 "remain": 12.0 if i == 1 else 0.0, "charges": None},
                {"slot": 13, "idx": 2, "id": 3270001687, "class": "CCitadel_Item_WarpStone",
                 "start": 80.0, "end": 83.0,
                 "remain": 3.0 if i == 1 else 0.0, "charges": 1},
                {"slot": 14, "idx": 3, "id": 381961617, "class": "CCitadel_Item_ActiveReload",
                 "remain": 7.0, "charges": None},
            ]},
            # spread over the plate the way a real match is: amber pushes up the
            # left lanes, sapphire comes down the right ones
            "position": ([(-8000 + 2600 * i), (-6500 + 900 * i), 100.0] if i < 6
                         else [(8400 - 2600 * (i - 6)), (6500 - 900 * (i - 6)), 100.0]),
        })
    players.append({"controller": 999, "slot": 0, "team": "Spectator",
                    "hero_id": None, "name": "caster", "is_spectator": True})
    return {
        "attached": True,
        "snapshot": {
            "match_id": 424242,
            "game_state": "GameInProgress",
            "context": "Match",
            "clock": {"now": 754.5, "game_start_time": 0.0, "total_paused_ticks": 0,
                      "tick_rate": 64.0},
            "paused": False,
            "teams": [
                {"team": "Amber", "score": 5, "souls": 51200, "kills": 14, "rejuvenators": 2},
                {"team": "Sapphire", "score": 3, "souls": 48900, "kills": 11, "rejuvenators": 0},
            ],
            "players": players,
            "objectives": objectives(),
            "timers": {
                "match_time": 754.5,
                "midboss": {"present": True, "count": 1, "next_spawn_in": None},
                "midboss_state": {"health": 8400, "max_health": 8400},
                "urn": {"present": True, "count": 1, "next_spawn_in": 25.0},
                "urn_location": [3200.0, -1200.0, 220.0],
                "rift": {"present": True, "count": 1, "next_spawn_in": None},
                "rift_state": {"health": 1200, "max_health": 1200},
                "bridge_buffs": {"next_spawn_in": 300.0},
            },
            "entity_count": 1234,
        },
    }


DOC = json.dumps(fixture()).encode()


class Bridge(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(DOC)))
        self.end_headers()
        self.wfile.write(DOC)


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode())


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def kill_diff():
    """two polls with one kill and one death: the pair should be linked."""
    import hub

    store = hub.Store()
    store.mem_mode = True
    first = fixture()
    second = fixture()
    second["snapshot"]["players"][2]["kills"] += 1
    second["snapshot"]["players"][7]["deaths"] += 1
    store.apply_mem(json.loads(json.dumps(first)))
    store.apply_mem(json.loads(json.dumps(second)))
    kills = store.state().get("kills") or []
    return kills[0] if kills else None


def map_projection():
    """the world -> plate formula, checked against two published camp positions.

    values come from /v1/assets/map: camp at (-9248, 0) sits at left 0.0699 /
    top 0.5, camp at (3248, -1888) at 0.6510 / 0.5878. the js in dlock.js
    (mapPct) is the same arithmetic.
    """
    def pct(x, y, radius=10752.0):
        span = radius * 2
        return (round((50 + x / span * 100) / 100, 4), round((50 - y / span * 100) / 100, 4))

    return [pct(-9248.0, 0.0), pct(3248.0, -1888.0)]


def mid_chip():
    """the MID chip follows the health bar moving, not the spawn timer."""
    import hub

    store = hub.Store()
    store.mem_mode = True
    store.apply_mem(json.loads(json.dumps(fixture())))
    fresh = store.timers_view()["midboss"].get("changing")
    store.mid_hp_at = hub.now() - 20
    stale = store.timers_view()["midboss"].get("changing")
    return fresh, stale


def kill_diff_tie():
    """two killers and two deaths in one window: deaths only, no invented pair."""
    import hub

    store = hub.Store()
    store.mem_mode = True
    first = fixture()
    second = fixture()
    second["snapshot"]["players"][2]["kills"] += 1
    second["snapshot"]["players"][4]["kills"] += 1
    second["snapshot"]["players"][7]["deaths"] += 1
    second["snapshot"]["players"][9]["deaths"] += 1
    store.apply_mem(json.loads(json.dumps(first)))
    store.apply_mem(json.loads(json.dumps(second)))
    return [(k["kind"], k["attacker"], k["victim"]) for k in store.state().get("kills") or []]


def kill_diff_spree():
    """one player takes two of three kills: still a spree line, deaths stand alone."""
    import hub

    store = hub.Store()
    store.mem_mode = True
    first = fixture()
    second = fixture()
    second["snapshot"]["players"][2]["kills"] += 2
    second["snapshot"]["players"][4]["kills"] += 1
    for i in (7, 9, 10):
        second["snapshot"]["players"][i]["deaths"] += 1
    store.apply_mem(json.loads(json.dumps(first)))
    store.apply_mem(json.loads(json.dumps(second)))
    return [(k["kind"], k["attacker"], k["victim"]) for k in store.state().get("kills") or []]


def hotkey_bits():
    """the combo parser and the config normaliser, without touching the OS."""
    import hub

    cfg = {"hotkeys": {}}
    hub.apply_config(cfg, {"hotkeys": {"compact": "Ctrl+Alt+1"}})
    ok_spec = cfg["hotkeys"].get("compact")
    hub.apply_config(cfg, {"hotkeys": {"compact": "no such key"}})
    dropped = cfg["hotkeys"].get("compact")
    return hub.parse_hotkey("NumpadAdd"), ok_spec, dropped, hub.parse_hotkey("nonsense")


def custom_name():
    """a name typed on the panel wins; clearing it falls back to the game's name."""
    import copy
    import hub

    st = hub.Store()
    st.mem_mode = True
    st.apply_mem(copy.deepcopy(fixture()))
    row = st.players()[0]
    key, raw = row["key"], row["rawName"]
    st.config["names"] = {key: "FAKER"}
    st.apply_mem(copy.deepcopy(fixture()))
    over = st.players()[0]
    st.config["names"] = {}
    st.apply_mem(copy.deepcopy(fixture()))
    back = st.players()[0]
    return (over["name"], over["rawName"], back["name"], raw)


def cd_bridge_ult():
    # the entity's cooldown end is the truth; the snapshot field only names the slot
    import copy
    import hub

    st = hub.Store()
    st.mem_mode = True
    doc = copy.deepcopy(fixture())
    hero = doc["snapshot"]["players"][0]["hero_id"]
    p = doc["snapshot"]["players"][0]
    p["ultimate_trained"] = True
    p["ultimate_cooldown_end"] = 100.0
    p["cooldowns"] = {"items": [], "abilities": [
        {"slot": 0, "class": "CCitadel_Ability_A", "end": 10.0, "remain": 0.0},
        {"slot": 1, "class": "CCitadel_Ability_B", "end": 100.0, "remain": 12.0},
        {"slot": 2, "class": "CCitadel_Ability_C", "end": 0.0, "remain": 0.0},
        {"slot": 3, "class": "CCitadel_Ability_D", "end": 5.0, "remain": 0.0},
    ]}
    doc["snapshot"]["clock"]["now"] = 88.0
    st.apply_mem(doc)
    return st, hero


def cd_pause():
    """A pause stops the match clock but not the clock we read, and the game does not
    push cooldown ends forward while the match is paused - so the hub has to hold the
    bars still instead of letting them drain on screen."""
    import copy

    import hub

    store = hub.Store()
    store.mem_mode = True
    doc = copy.deepcopy(fixture())
    doc["snapshot"]["pause"] = {"game_paused": True, "server_paused": False}
    p = doc["snapshot"]["players"][0]
    p["cooldowns"] = {"items": [], "abilities": [
        {"slot": 1, "class": "CCitadel_Ability_B", "end": 100.0, "remain": 12.0},
    ]}
    p["ultimate_cooldown_end"] = 100.0
    p["ultimate_trained"] = True
    store.ult_slot[str(p["hero_id"])] = 1

    def ult():
        return [q for q in store.state()["players"] if q["ctrl"] == p["slot"]][0]["cd"]["ult"]["remain"]

    store.apply_mem(doc)
    first = ult()

    # the pause drags on and the entity starts reporting less time left
    doc["snapshot"]["clock"]["now"] += 5.0
    p["cooldowns"]["abilities"][0]["remain"] = 7.0
    store.apply_mem(doc)
    during = ult()

    # back to play: the new number is allowed through again
    doc["snapshot"]["pause"] = {"game_paused": False, "server_paused": False}
    p["cooldowns"]["abilities"][0]["remain"] = 6.0
    store.apply_mem(doc)
    after = ult()
    return (first, during, after)


def cd_pause_isolated():
    """Two paused players must keep their own item rows.

    The pause path caches the block per player; keying it on (team, ctrl) collapsed
    them into one entry while a paused client blanks those fields, which put the top
    player's items on everybody.
    """
    import copy

    import hub

    store = hub.Store()
    store.mem_mode = True
    doc = copy.deepcopy(fixture())
    doc["snapshot"]["pause"] = {"game_paused": True, "server_paused": False}
    a = doc["snapshot"]["players"][0]
    a["items"] = [1976391348]
    a["cooldowns"] = {"items": [{"slot": 3, "class": "CCitadel_Item_ColdFront",
                                 "id": 1976391348, "start": 80.0, "end": 104.0,
                                 "remain": 12.0}], "abilities": []}
    b = doc["snapshot"]["players"][1]
    b["items"] = [3270001687]
    b["cooldowns"] = {"items": [{"slot": 3, "class": "CCitadel_Item_WarpStone",
                                 "id": 3270001687, "start": 80.0, "end": 96.0,
                                 "remain": 9.0}], "abilities": []}
    a["name"], b["name"] = "alpha", "beta"

    store.apply_mem(doc)
    rows = {p["name"]: [i["id"] for i in p["cd"]["items"]] for p in store.state()["players"]}
    return (rows.get("alpha"), rows.get("beta"))


def struct_unknown():
    """A structure the client cannot read is not a destroyed one, but it is not a
    confirmed standing one either - the widget shows a '?' for that."""
    import hub

    store = hub.Store()
    store.mem_mode = True
    doc = fixture()
    for o in doc["snapshot"]["objectives"]:
        if o["kind"] == "guardian" and o["team"] == "Amber" and o["state"] == "standing":
            o["state"] = "unknown"
            break
    store.apply_mem(json.loads(json.dumps(doc)))
    teams = store.state()["teams"]
    return teams[1]["structuresDown"], teams[1]["structuresKnown"]


def all_down():
    """every enemy structure is gone: the bar must show the full count, not a dash."""
    import hub

    store = hub.Store()
    store.mem_mode = True
    doc = fixture()
    doc["snapshot"]["objectives"] = [
        o for o in doc["snapshot"]["objectives"]
        if not (o.get("team") == "Sapphire"
                and o.get("kind") in ("guardian", "walker", "base_guardian", "shrine", "patron"))
    ]
    store.apply_mem(doc)
    teams = store.state()["teams"]
    got = [t.get("structuresDown") for t in teams]
    want = [16, 3]
    ok = got == want
    print("%-18s %-8s got=%r want=%r" % ("towers wiped", "ok" if ok else "FAIL", got, want))
    return ok


def patron_phase():
    """first patron phase dying is worth one tower, so the bar reads 15 then 16."""
    import hub

    store = hub.Store()
    store.mem_mode = True
    doc = fixture()
    store.apply_mem(json.loads(json.dumps(doc)))
    before = store.state()["teams"][0]["structuresDown"]

    # the patron has lost its first phase: the client simply stops reporting it
    doc["snapshot"]["objectives"] = [o for o in doc["snapshot"]["objectives"]
                                    if not (o.get("team") == "Sapphire"
                                            and o.get("kind") == "patron")]
    store.apply_mem(json.loads(json.dumps(doc)))
    after = store.state()["teams"][0]["structuresDown"]

    got = (before, after)
    want = (1, 2)
    ok = got == want
    print("%-18s %-8s got=%r want=%r" % ("patron phase", "ok" if ok else "FAIL", got, want))
    return ok


def keys_slots():
    """the strip's cell list: twelve keys, scoreboard order, holes stay holes."""
    import keys_window
    import hub

    store = hub.Store()
    store.mem_mode = True
    store.apply_mem(json.loads(json.dumps(fixture())))
    state = store.state()
    cells = keys_window.slots(state)
    short = keys_window.slots({"players": state["players"][:5]})
    swapped = keys_window.swap_order(list(range(12)), 0, 6)
    return (
        len(cells),
        "".join(c["key"] for c in cells),
        cells[0]["hero"],
        cells[6]["hero"],
        state["players"][0]["hero"],
        state["players"][6]["hero"],
        [c["empty"] for c in short],
        swapped,
    )



def main():
    bridge = ThreadingHTTPServer(("127.0.0.1", 8799), Bridge)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    here = os.path.dirname(os.path.abspath(__file__))
    hub = subprocess.Popen([sys.executable, "hub.py", "--port", "8790", "--no-keys",
                            "--bridge", "http://127.0.0.1:8799/"], cwd=here,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            try:
                get("http://127.0.0.1:8790/api/state")
                break
            except Exception:
                time.sleep(0.25)
        time.sleep(1.2)
        st = get("http://127.0.0.1:8790/api/state")
        checks = [
            ("phase", st.get("phase"), "live"),
            ("matchId", st.get("matchId"), 424242),
            ("clock", round(st.get("clock") or 0), 755 if round(st.get("clock") or 0) == 755 else round(st.get("clock") or 0)),
            ("players", len(st.get("players") or []), 12),
            ("spectators dropped", all(p["team"] in (2, 3) for p in st["players"]), True),
            ("scores", [t.get("score") for t in st["teams"]], [5, 3]),
            ("netWorth", [t.get("netWorth") for t in st["teams"]], [45000, 81000]),
            ("hp", st["players"][1].get("hp"), 900),
            ("hero name", st["players"][1].get("heroName"), "Haze"),
            ("level", st["players"][1].get("level"), 11),
            ("items on p1", st["players"][1].get("items"), [1976391348, 3270001687, 381961617]),
            ("cd item id", [(i.get("id"), i.get("remain")) for i in
                            (st["players"][1].get("cd") or {}).get("items") or []],
             [(1976391348, 12.0), (3270001687, 3.0)]),
            ("cd item total", [i.get("total") for i in
                               (st["players"][1].get("cd") or {}).get("items") or []], [24.0, 3.0]),
            ("passive dropped", 381961617 in [i.get("id") for i in
                                              (st["players"][1].get("cd") or {}).get("items") or []],
             False),
            ("cd ult remain", ((st["players"][1].get("cd") or {}).get("ult") or {}).get("remain"), 90.0),
            ("cd ult ready false", ((st["players"][1].get("cd") or {}).get("ult") or {}).get("ready"), False),
            ("cd ult ready true", ((st["players"][0].get("cd") or {}).get("ult") or {}).get("ready"), True),
            ("urn countdown", (st.get("timers") or {}).get("urn", {}).get("in"), 25.0),
            ("midboss hp", (st.get("timers") or {}).get("midboss", {}).get("hp"), 8400),
            ("rift present", (st.get("timers") or {}).get("rift", {}).get("present"), True),
            ("map radius", (st.get("map") or {}).get("radius"), 10752),
            ("map objectives", len((st.get("map") or {}).get("objectives") or []), 30),
            ("map midboss", bool((st.get("map") or {}).get("midboss")), True),
            ("map urn", bool((st.get("map") or {}).get("urn")), True),
            ("player x", st["players"][0].get("x"), -8000.0),
            ("series", st.get("series"), {"bo": 3, "a": 0, "b": 0}),
            ("team kills", [t.get("kills") for t in st["teams"]], [14, 11]),
            ("towers down", [t.get("structuresDown") for t in st["teams"]], [1, 3]),
            ("towers total", [t.get("structuresTotal") for t in st["teams"]], [16, 16]),
            ("towers known", [t.get("structuresKnown") for t in st["teams"]], [True, True]),
            ("logo default", [t.get("logo") for t in st["teams"]], ["", ""]),
        ("rejuv", [t.get("rejuv") for t in st["teams"]], [2, 0]),
        ]
        bad = 0
        for name, got, want in checks:
            ok = got == want
            bad += 0 if ok else 1
            print("%-18s %-8s got=%r want=%r" % (name, "ok" if ok else "FAIL", got, want))
        print("players:", [(p["ctrl"], p["team"], p["name"], p["heroName"], p["netWorth"]) for p in st["players"]][:3])
        k = kill_diff()
        want = ("player2", "player7")
        got = (k or {}).get("attacker"), (k or {}).get("victim")
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("kill pairing", "ok" if ok else "FAIL", got, want))

        tie = kill_diff_tie()
        got = sorted((kind, victim) for kind, _, victim in tie)
        want = [("death", "player7"), ("death", "player9")]
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("kill tie", "ok" if ok else "FAIL", got, want))

        spree = kill_diff_spree()
        got = [(kind, attacker, victim) for kind, attacker, victim in spree]
        want = [("spree", "player2", None), ("death", None, "player10"),
                ("death", None, "player9"), ("death", None, "player7")]
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("kill spree", "ok" if ok else "FAIL", got, want))

        got = mid_chip()
        want = (True, False)
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("mid chip", "ok" if ok else "FAIL", got, want))

        got = map_projection()
        want = [(0.0699, 0.5), (0.651, 0.5878)]
        close = all(abs(a[0] - b[0]) < 1e-4 and abs(a[1] - b[1]) < 1e-4
                    for a, b in zip(got, want))
        ok = close
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("map projection", "ok" if ok else "FAIL", got, want))

        cfg = post("http://127.0.0.1:8790/api/config", {"bo": 5})
        got = (cfg.get("config") or {}).get("bo")
        ok = got == 5
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("config bo", "ok" if ok else "FAIL", got, 5))

        cfg = (post("http://127.0.0.1:8790/api/config", {"bo": 3, "w0": 9, "w1": 2})
               .get("config") or {})
        got = (cfg.get("w0"), cfg.get("w1"))
        want = (2, 2)
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("wins clamped", "ok" if ok else "FAIL", got, want))

        cfg = (post("http://127.0.0.1:8790/api/config", {"logo0": "../secret"})
               .get("config") or {})
        got = cfg.get("logo0")
        ok = got == ""
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("logo rejected", "ok" if ok else "FAIL", got, ""))

        cfg = (post("http://127.0.0.1:8790/api/config",
                    {"logo0": "preview/scoreboard.png"}).get("config") or {})
        got = cfg.get("logo0")
        want = "preview/scoreboard.png"
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("logo accepted", "ok" if ok else "FAIL", got, want))

        st, hero = cd_bridge_ult()
        row = st.state()["players"][0]
        got = row["cd"]["ult"]["remain"]
        want = 12.0
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("ult from entity", "ok" if ok else "FAIL", got, want))

        got = st.ult_slot.get(str(hero))
        want = 1
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("ult slot learned", "ok" if ok else "FAIL", got, want))

        got = cd_pause()
        want = (12.0, 12.0, 6.0)
        ok = got == want

        got = cd_pause_isolated()
        want = ([1976391348], [3270001687])
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("cd pause isolated", "ok" if ok else "FAIL", got, want))

        got = cd_pause()
        want = (12.0, 12.0, 6.0)
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("cd frozen on pause", "ok" if ok else "FAIL", got, want))

        got = custom_name()
        want = ("FAKER", "player0", "player0", "player0")
        ok = got[0] == "FAKER" and got[2] == got[3]
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("custom name", "ok" if ok else "FAIL", got, want))

        parsed, ok_spec, dropped, junk = hotkey_bits()
        got = (parsed, ok_spec, dropped, junk)
        want = ((0x4000, 0x6B), "Ctrl+Alt+1", None, None)
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("hotkey parse", "ok" if ok else "FAIL", got, want))

        got = struct_unknown()
        want = (3, False)
        ok = got == want
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("struct unknown", "ok" if ok else "FAIL", got, want))

        n, keysel, h0, h6, want0, want6, empties, swapped = keys_slots()
        want = (12, "1234567890-=", want0, want6)
        ok = n == 12 and keysel == "1234567890-=" and h0 == want0 and h6 == want6
        ok = ok and empties[:5] == [False] * 5 and all(empties[5:])
        ok = ok and swapped == [6, 1, 2, 3, 4, 5, 0, 7, 8, 9, 10, 11]
        bad += 0 if ok else 1
        print("%-18s %-8s got=%r want=%r" % ("keys slots", "ok" if ok else "FAIL",
                                             (n, keysel, h0, h6), want))

        ok = all_down()
        bad += 0 if ok else 1

        ok = patron_phase()
        bad += 0 if ok else 1

        return 1 if bad else 0
    finally:
        hub.terminate()
        bridge.shutdown()


if __name__ == "__main__":
    sys.exit(main())
