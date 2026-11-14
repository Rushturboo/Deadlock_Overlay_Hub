//! dl-live-bridge: dumps what the game has in memory as JSON on 127.0.0.1:8788.
//!
//! The snapshot comes straight from `deadlock-reader`. On top of it this file
//! adds the one thing the reader does not expose: per-player *ability cooldowns*
//! (the six item slots), which live on the pawn's ability component.
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use deadlock_reader::Reader;
use serde_json::{json, Value};

const POLL: Duration = Duration::from_millis(200);
const IDLE: Duration = Duration::from_millis(250);
const RETRY: Duration = Duration::from_secs(2);
/// the entity table only changes when something spawns, so one walk a second is
/// plenty; the cooldown *values* are still read every poll
const WALK_EVERY: Duration = Duration::from_secs(1);
/// the item slots sit at the end of m_vecAbilities; anything past this is junk
const MAX_SLOTS: u32 = 32;

fn store(state: &Mutex<String>, body: String) {
    if let Ok(mut slot) = state.lock() {
        *slot = body;
    }
}

fn serve(mut stream: TcpStream, state: &Mutex<String>) {
    let mut head = [0u8; 1024];
    let _ = stream.read(&mut head);
    let body = state.lock().map(|s| s.clone()).unwrap_or_else(|_| String::from("{}"));
    let head = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
         Access-Control-Allow-Origin: *\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(body.as_bytes());
    let _ = stream.flush();
}

/// Entity index -> (class, instance) plus a cache of schema offsets, so the hot
/// loop does not re-resolve the same two lookups 288 times a second.
#[derive(Default)]
struct Shadow {
    ents: HashMap<u32, (String, u64)>,
    walked_at: Option<Instant>,
    offsets: HashMap<(String, String), Option<u32>>,
}

impl Shadow {
    fn offset(&mut self, reader: &Reader, class: &str, field: &str) -> Option<u32> {
        let key = (class.to_string(), field.to_string());
        if let Some(hit) = self.offsets.get(&key) {
            return *hit;
        }
        let found = reader.offset_of(class, field);
        self.offsets.insert(key, found);
        found
    }

    fn walk(&mut self, reader: &Reader) {
        let stale = self
            .walked_at
            .map(|t| t.elapsed() >= WALK_EVERY)
            .unwrap_or(true);
        if !stale {
            return;
        }
        if let Ok(snap) = reader.entities() {
            self.ents.clear();
            for e in snap.all() {
                let name = e.best_name();
                if name.starts_with("C_Citadel")
                    || name.starts_with("CCitadel")
                    || name.starts_with("CItem")
                {
                    self.ents.insert(e.index as u32, (name.to_string(), e.instance));
                }
            }
            self.walked_at = Some(Instant::now());
        }
    }
}

/// The item slots of one pawn, plus the ultimate, with remaining cooldowns in
/// match seconds. An unfilled slot reads `CCitadel_Item_Empty` and is dropped.
/// Item classes show up under two names: `CCitadel_Item_X` and `CItem_X`, so the
/// prefix test has to cover both or half the actives silently disappear.
fn item_cooldowns(
    reader: &Reader,
    shadow: &mut Shadow,
    pawn_idx: u32,
    clock: f32,
) -> (Vec<Value>, Vec<Value>) {
    let mut out = Vec::new();
    let mut abilities: Vec<Value> = Vec::new();
    let Some((pawn_class, pawn_inst)) = shadow.ents.get(&pawn_idx).cloned() else {
        return (out, abilities);
    };
    if !pawn_class.ends_with("PlayerPawn") {
        return (out, abilities);
    }
    let Some(comp_off) = shadow.offset(reader, &pawn_class, "m_CCitadelAbilityComponent") else {
        return (out, abilities);
    };
    let vec_off = shadow
        .offset(reader, "CCitadelAbilityComponent", "m_vecAbilities")
        .unwrap_or(0x68);
    let mem = reader.memory();
    let vec = pawn_inst + comp_off as u64 + vec_off as u64;
    let size = mem.read_u32(vec).unwrap_or(0);
    let ptr = mem.read_u64(vec + 8).unwrap_or(0);
    if size == 0 || size > MAX_SLOTS || ptr < 0x1000 {
        return (out, abilities);
    }

    let mut ordinal = 0;
    for i in 0..size {
        let Ok(handle) = mem.read_u32(ptr + (i as u64) * 4) else {
            continue;
        };
        let Some((class, inst)) = shadow.ents.get(&(handle % 16384)).cloned() else {
            continue;
        };
        // slots 0..4 are the hero abilities in cast order. which one is the
        // ultimate differs per hero, so ship all four and let the hub line them
        // up against the snapshot's ultimate_cooldown_end
        if i < 4 && class.starts_with("CCitadel_Ability_") {
            let end = shadow
                .offset(reader, &class, "m_flCooldownEnd")
                .and_then(|o| mem.read_u32(inst + o as u64).ok().map(f32::from_bits));
            let start = shadow
                .offset(reader, &class, "m_flCooldownStart")
                .and_then(|o| mem.read_u32(inst + o as u64).ok().map(f32::from_bits));
            if let Some(end) = end {
                abilities.push(json!({
                    "slot": i,
                    "class": class,
                    "start": start,
                    "end": end,
                    "remain": (end - clock).max(0.0),
                }));
            }
        }
        // item abilities are not all called CCitadel_Item_*: actives also show up as
        // CItem*, CCitadel_WeaponUpgrade_* (Cultist Sacrifice) and
        // CCitadel_ArmorUpgrade_*. take anything that looks like an item/upgrade and
        // let the hub decide which of them are the four button slots
        let itemish = class.starts_with("CCitadel_Item_")
            || class.starts_with("CItem")
            || class.contains("Upgrade");
        if !itemish {
            continue;
        }
        // "idx" is the position among the six item slots; "id" is the real item
        // id from m_nSubclassID, which is the only reliable way to line a
        // cooldown up with what the inventory list shows
        let idx = ordinal;
        ordinal += 1;
        if class.ends_with("_Empty") {
            continue;
        }
        // subclasses do not always publish m_nSubclassID in their own schema, so fall
        // back to the offset the base item class uses (probe9: +0x388)
        let subclass = shadow
            .offset(reader, &class, "m_nSubclassID")
            .or(Some(0x388))
            .and_then(|o| mem.read_u32(inst + o as u64).ok());
        let end = shadow
            .offset(reader, &class, "m_flCooldownEnd")
            .and_then(|o| mem.read_u32(inst + o as u64).ok().map(f32::from_bits));
        let start = shadow
            .offset(reader, &class, "m_flCooldownStart")
            .and_then(|o| mem.read_u32(inst + o as u64).ok().map(f32::from_bits));
        let charges = shadow
            .offset(reader, &class, "m_iRemainingCharges")
            .and_then(|o| mem.read_u32(inst + o as u64).ok());
        // the client keeps a second, frozen copy of a cooldown that was stopped (a
        // pause, or an ability interrupted mid-cast). exposing both lets the hub pick
        // whichever still has a live value.
        let stopped = shadow
            .offset(reader, &class, "m_flStoppedCooldownRemaining")
            .and_then(|o| mem.read_u32(inst + o as u64).ok().map(f32::from_bits));
        let remain = end.map(|e| (e - clock).max(0.0));
        out.push(json!({
            "slot": i,
            "idx": idx,
            "id": subclass,
            "class": class,
            "start": start,
            "end": end,
            "remain": remain,
            "stopped": stopped,
            "charges": charges,
        }));
    }
    (out, abilities)
}

fn decorate(reader: &Reader, shadow: &mut Shadow, snapshot: &Value) -> Value {
    let mut value = snapshot.clone();
    shadow.walk(reader);
    let clock = snapshot
        .get("clock")
        .and_then(|c| c.get("now"))
        .and_then(|n| n.as_f64())
        .unwrap_or(0.0) as f32;
    if let Some(players) = value.get_mut("players").and_then(|p| p.as_array_mut()) {
        for player in players.iter_mut() {
            let Some(handle) = player.get("pawn_handle").and_then(|h| h.as_u64()) else {
                continue;
            };
            let (items, abilities) = item_cooldowns(reader, shadow, (handle % 16384) as u32, clock);
            player["cooldowns"] = json!({ "items": items, "abilities": abilities });
        }
    }
    value
}

fn reader_loop(state: Arc<Mutex<String>>) {
    let mut reader: Option<deadlock_reader::Reader> = None;
    let mut shadow = Shadow::default();
    let mut last_note: Option<String> = None;
    loop {
        if reader.is_none() {
            match deadlock_reader::Reader::attach() {
                Ok(r) => {
                    eprintln!("attached to deadlock.exe pid {}", r.pid());
                    last_note = None;
                    shadow = Shadow::default();
                    reader = Some(r);
                }
                Err(e) => {
                    // the game is usually just not running yet: say it once instead
                    // of twice a second, or the console turns into wallpaper
                    let note = format!("{e}");
                    if last_note.as_deref() != Some(note.as_str()) {
                        eprintln!("attach failed: {note} (waiting for the game)");
                        last_note = Some(note);
                    }
                    store(&state, String::from("{\"attached\":false,\"phase\":\"game_closed\"}"));
                    thread::sleep(RETRY);
                    continue;
                }
            }
        }
        let r = reader.as_mut().expect("reader present");
        match r.live_snapshot() {
            Ok(Some(snapshot)) => {
                let doc = match serde_json::to_value(&snapshot) {
                    Ok(v) => json!({"attached": true, "snapshot": decorate(r, &mut shadow, &v)}),
                    Err(e) => {
                        eprintln!("serialize failed: {e}");
                        thread::sleep(POLL);
                        continue;
                    }
                };
                match serde_json::to_string(&doc) {
                    Ok(text) => store(&state, text),
                    Err(e) => eprintln!("serialize failed: {e}"),
                }
            }
            Ok(None) => store(&state, String::from("{\"attached\":true,\"snapshot\":null}")),
            Err(e) => {
                let note = format!("{e}");
                if last_note.as_deref() != Some(note.as_str()) {
                    eprintln!("snapshot failed: {note}");
                    last_note = Some(note);
                }
                reader = None;
                thread::sleep(RETRY);
                continue;
            }
        }
        thread::sleep(POLL);
    }
}

fn main() {
    let state = Arc::new(Mutex::new(String::from(
        "{\"attached\":false,\"phase\":\"game_closed\"}",
    )));
    let writer = state.clone();
    thread::spawn(move || reader_loop(writer));

    let port: u16 = std::env::args()
        .nth(1)
        .and_then(|a| a.parse().ok())
        .unwrap_or(8788);
    let listener = TcpListener::bind(("127.0.0.1", port)).expect("bind 127.0.0.1");
    eprintln!("bridge on http://127.0.0.1:{port}/  (game must be running)");
    for stream in listener.incoming().flatten() {
        let shared = state.clone();
        thread::spawn(move || {
            serve(stream, &shared);
            thread::sleep(IDLE);
        });
    }
}
