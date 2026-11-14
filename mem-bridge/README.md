# mem-bridge

Reads the running `deadlock.exe` and prints one JSON snapshot on
`http://127.0.0.1:8788/`. `hub.py` polls it five times a second.

It links [`deadlock-reader`](https://github.com/deadlock-plus/deadlock-rs), which does
the actual memory walking (schemas, entity lists, field offsets). The reader is a normal
crate, so the only thing to wire up is the path in `Cargo.toml`:

```toml
[dependencies]
deadlock-reader = { path = "../../Deadlock+SourceCode/deadlock-rs-<rev>/crates/deadlock-reader",
                    features = ["serde", "positions"] }
```

Point that at your own checkout of `deadlock-plus/deadlock-rs` (any recent revision that
still has `crates/deadlock-reader`), then:

```bat
cargo build --release
```

The binary lands in `target\release\dl-live-bridge.exe`. `start-hub.cmd` starts it for
you, hidden; you should never have to run it by hand.

## what it adds on top of the raw snapshot

The reader already gives players, teams, objectives, timers and item ids. The bridge
adds `player["cooldowns"]`:

```json
{
  "items":     [{"slot": 12, "class": "CCitadel_Item_WarpStone", "id": 3270001687,
                 "start": 1044.3, "end": 1060.3, "remain": 10.1, "charges": null}],
  "abilities": [{"slot": 20, "class": "CCitadel_Ability_CardToss",
                 "start": 1000.0, "end": 1170.0, "remain": 120.0}]
}
```

`id` is `m_nSubclassID`, the item id the shop and the icon endpoints use. `remain` is
`end - clock`, both taken from the same snapshot, so it never drifts.
