# Deadlock Overlay Hub

A local broadcast overlay for **Deadlock** esports streams. Everything on screen is
driven by the game itself: the hub reads the running `deadlock.exe` through a small
Rust bridge and pushes one JSON state object to four OBS browser sources.

No API keys, no match ids, no spectate quota, no recorded demos — start your game and
the overlays follow it.

```
┌──────────────┐   memory    ┌────────────────┐  http 127.0.0.1:8787  ┌──────────┐
│ deadlock.exe │ ──────────► │ dl-live-bridge │ ────────────────────► │   hub    │
└──────────────┘  5 Hz poll  └────────────────┘       JSON snapshot   └────┬─────┘
                                                                           │ /api/state
                                    ┌──────────────────────────────────────┴────────┐
                                    ▼              ▼              ▼               ▼
                              scoreboard.html  players.html  killfeed.html   minimap.html
```

## what is in the box

| file | what it does |
|---|---|
| `hub.py` | the local server: reads the bridge, keeps the state, serves the pages, owns the hotkey and the floating strip |
| `mem-bridge/` | Rust program that reads `deadlock.exe` memory and exposes one JSON snapshot on `127.0.0.1:8788` |
| `scoreboard.html` | 940×160 top bar: series wins, crests, team names, towers, souls, kills, clock, objective chips, rejuv marker |
| `players.html` | 1920×1080 two columns of 6: portrait, name, hero, souls, KDA, health, 12 item slots, and a compact mode with ult + active item cooldowns |
| `killfeed.html` | 900×560 corner feed: kill / died / most kills lines |
| `minimap.html` | 340×340 drawn map: player portraits with smooth motion, objectives, midboss, soul urn |
| `keys_window.py` | floating strip over the game: spectate key `1…0 - =` next to each player's portrait |
| `control.html` | the panel at `http://127.0.0.1:8787/` |
| `data/` | generated at runtime: downloaded art (portraits, item icons, map plate), `items.json` / `heroes.json` and local state; git-ignored, see `data/README.md` |

## requirements

- Windows (the bridge reads a Windows process)
- Python 3.10+ (standard library only, no pip installs)
- Rust toolchain (only to build the bridge, once)
- OBS Studio with four **Browser** sources

## build the bridge (once)

The bridge links [`deadlock-plus/deadlock-rs`](https://github.com/deadlock-plus/deadlock-rs)
(MIT OR Apache-2.0). `mem-bridge/Cargo.toml` points at a local checkout:

```toml
deadlock-reader = { path = "../../Deadlock+SourceCode/deadlock-rs-<rev>/crates/deadlock-reader", ... }
```

Clone that repository and edit the path to match where you put it, then:

```bat
cd mem-bridge
cargo build --release
```

That produces `mem-bridge\target\release\dl-live-bridge.exe`.

## run

```bat
start-hub.cmd
```

The hub starts the bridge itself (hidden), serves the panel on <http://127.0.0.1:8787/>
and opens the browser. The bridge follows whatever the game is doing: menus, hero pick,
a live match, the post game screen.

Options: `hub.py --port 9000`, `--bridge http://127.0.0.1:8788/`, `--no-keys`, `--no-kills`.

## OBS sources

Four Browser sources, **not** "local file":

| source | size | url |
|---|---|---|
| scoreboard | 940×160 | `http://127.0.0.1:8787/scoreboard.html` |
| players | 1920×1080 | `http://127.0.0.1:8787/players.html` |
| killfeed | 900×560 | `http://127.0.0.1:8787/killfeed.html` |
| minimap<br />(This thing is developing and the effect is not so good now, i recommend to use the map in game) | 340×340 | `http://127.0.0.1:8787/minimap.html` |

Tick **Shutdown source when not visible** off. If a page ever looks stale, the hub
bumps a stamp whenever a widget file changes and the pages reload themselves; if you
edit `hub.py` itself, restart the hub.

## the panel

- **match** — title line above the clock, team names, best-of, series wins, crest paths
  (relative to this folder, e.g. `logos/T1.png`), a **Pause** button that stops reading
  the game.
- **players layout** — flips `players.html` between long bars and the compact squares
  with the ability panel (also on the `NumpadAdd` hotkey).
- **hotkeys** — one global hotkey, registered with `RegisterHotKey`, so it works with
  the game in front. Empty the box to unbind.
- **keys window** — restart / hide the floating strip.
- **sync** — `overlay delay (ms)`: shift the clock back to match a delayed video feed.
- **roster** — every player the hub found, with the in-game name and a custom name you
  can type (empty = in-game name).
- **Stop everything** — stops the hub, kills the bridge and closes the strip.

## the floating key strip

`keys_window.py` draws twelve cells: spectate key on top, hero portrait below, in
scoreboard order. Drag it anywhere (the position is saved), double click to close.

When the game reshuffles the spectate order, hold `` ` `` (the key left of `1`) and
press the two keys you want to swap **(We should do this because for now on we can only get what characters is in the game but the position from the observer control is not match to the window. So you should do this little work at the beginning of the game :D)** ; the strip only *watches* the keyboard
(`GetAsyncKeyState`), so every key still reaches the game and other applications.

## notes from building it

- The scoreboard tower count is `16 - towers still standing on the enemy side`
  (3 guardians + 3 walkers + 6 base towers + 2 shrines + 2 patron phases). Destroyed
  structures disappear from the entity list instead of being flagged, so the count is
  a subtraction.
- Cooldown wedges use the entity's own `m_flCooldownStart` / `m_flCooldownEnd` when the
  item has one, otherwise the shop's base cooldown from `data/items.json`. A few active
  items have no ability entity at all — those show an icon and no wedge.
- Kill attribution is not in the game's memory. The hub diffs the kill counters and, when
  more than one player gained a kill in the same window, shows "X died" plus the leader.
- Everything is re-read every 200 ms; a match pause freezes the cooldown wedges.

## credits

- [`deadlock-api`](https://github.com/deadlock-api/deadlock-api) — the static asset
  endpoints used for portraits, item icons and the map plate. those files are
  downloaded at runtime, not redistributed here.
- [`deadlock-plus`](https://github.com/deadlock-plus) — `deadlock-reader`, the memory
  reader the bridge links.

Reading another process's memory is your call; use it on your own machine.
