"""A strip that floats over the game: one cell per spectate key, portrait only.

The operator watches the game full screen, so this window has to sit on top of it
and never steal focus. It shows which key brings up which hero - nothing else.

Run it directly for a quick look:

    py -3 keys_window.py --hub http://127.0.0.1:8787

The hub starts it by itself in memory mode (and stops it again on quit), so in
normal use nobody types the command above.

tkinter is in the standard library, so this needs no extra packages. The window is
frameless, kept topmost, and the background colour is registered as transparent so
only the cells are visible over the game.
"""
import argparse
import ctypes
import io
import json
import queue
import os
import sys
import threading
import time
import urllib.request

import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(HERE, 'data', 'keys.pid')
POS_FILE = os.path.join(HERE, "data", "keys-window.json")
PORTRAITS = os.path.join(HERE, "data", "portraits")

# the twelve spectate keys, in the order the game cycles players
KEYS = "1234567890-="

PORTRAIT = 64          # source art is 128x128, so this is a clean subsample
PAD = 8
GAP = 6
TRANSPARENT = "#010203"
BG = "#0d1015"
KEY_FG = "#ffd166"
EMPTY_BG = "#171b21"
ARM_ON = "#7a5a1e"      # strip border while the swap key is held
ARM_PICK = "#ffd166"    # the first key picked
MODE_BARS = "#9aa6b4"   # the "players: normal" tile
MODE_COMPACT = "#45d97a"  # teamfight squares go green so it reads instantly


def slots(state):
    """Turn /api/state into the twelve cells, in scoreboard order.

    state comes from the hub; a player's `ctrl` is only 1..6 per team, so the
    order that matters is (team, ctrl) - the same one the scoreboard and the
    roster table use. Slots without a player stay in place, so the key legend
    never shifts around mid match.
    """
    players = list((state or {}).get("players") or [])
    players.sort(key=lambda p: (p.get("team") or 0, p.get("ctrl") if isinstance(p.get("ctrl"), int) else 99))
    out = []
    for i, key in enumerate(KEYS):
        p = players[i] if i < len(players) else None
        out.append({
            "key": key,
            "hero": (p or {}).get("hero"),
            "empty": p is None,
        })
    return out


def blank_order():
    """identity mapping: key i shows data slot i."""
    return list(range(len(KEYS)))


def swap_order(order, a, b):
    """swap the portraits shown under two keys (pure, so it can be tested)."""
    out = list(order or blank_order())
    if a == b or not (0 <= a < len(out)) or not (0 <= b < len(out)):
        return out
    out[a], out[b] = out[b], out[a]
    return out


def load_layout():
    """(x, y, order) from disk. any older file without an order stays identity."""
    try:
        with open(POS_FILE, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None, None, blank_order()
    order = doc.get("order")
    if not isinstance(order, list) or sorted(order) != blank_order():
        order = blank_order()
    x, y = doc.get("x"), doc.get("y")
    if not (isinstance(x, int) and isinstance(y, int)):
        x = y = None
    return x, y, order


def save_layout(x, y, order):
    try:
        os.makedirs(os.path.dirname(POS_FILE), exist_ok=True)
        with open(POS_FILE, "w", encoding="utf-8") as f:
            json.dump({"x": int(x), "y": int(y), "order": list(order)}, f)
    except OSError as exc:
        print("keys: could not save layout: %s" % exc, flush=True)


def save_order(order):
    """write the new key order but leave the position alone.

    a swap can fire while the window is not mapped (or right after a restart), and
    asking tk for its position then returns 0,0 - which would teleport the strip.
    """
    x, y, _ = load_layout()
    save_layout(x or 0, y or 0, order)


# --- swap mode ---------------------------------------------------------------
# the game rearranges who sits on which spectate key (players swap lanes, people
# reconnect), so the legend needs a way to be corrected without touching a config
# file. hold the key left of `1` (backtick / the ` key) and press two of the twelve
# keys: those two portraits trade places and the mapping is saved.
#
# the keys are swallowed while the modifier is held, otherwise the game would also
# jump to player 1 and player 7 while you are fixing the legend.

TRY_SWAP_VK = 0xC0          # VK_OEM_3, the ` / ~ key the user asked for

# No keyboard hook here on purpose.
#
# An earlier version installed a WH_KEYBOARD_LL hook and returned 1 for the keys
# it owned, which meant the OS stopped delivering them everywhere. When that hook
# thread got stuck the whole desktop lost keystrokes (alt+tab, win, everything)
# until the machine was power cycled. A legend strip is not worth global risk, so
# the strip only *reads* key state now: GetAsyncKeyState every 40ms on the tk
# thread, no hook, nothing swallowed. Every key still reaches the game and every
# other app; the trade-off is that pressing two keys while armed also makes the
# game jump the camera, which is harmless next to a frozen keyboard.



def key_vk(char):
    """virtual key code for one of the twelve spectate keys."""
    if char.isdigit():
        return 0x30 + int(char)
    return 0xBD if char == "-" else 0xBB          # OEM_MINUS / OEM_PLUS


VK_BY_CELL = [key_vk(c) for c in KEYS]


class Strip:
    def __init__(self, root, hub, scale=1.0):
        self.root = root
        self.hub = hub.rstrip("/")
        self.scale = scale
        self.cells = []
        self.images = {}        # hero id -> PhotoImage
        self.empty_img = None
        self.last = None
        self.ok = False
        self.order = blank_order()   # key -> which player slot it shows
        self.state = None
        self.armed = False           # the swap modifier is held down
        self.key_down = {}           # cell index -> was it down on the last poll
        self.picks = []              # cell indexes picked so far
        self.ui = queue.Queue()      # worker thread -> tk thread handoff
        self._closing = False
        self._build()

    # ---------- layout ----------

    def _portrait(self, hero):
        """Cached portrait for a hero id, or the empty tile when there is none."""
        if hero in self.images:
            return self.images[hero]
        path = os.path.join(PORTRAITS, "%s.png" % hero)
        img = None
        if isinstance(hero, int) and os.path.isfile(path):
            try:
                img = tk.PhotoImage(file=path)
                if img.width() > PORTRAIT:
                    img = img.subsample(max(1, img.width() // PORTRAIT))
            except tk.TclError:
                img = None
        if img is None:
            img = self.empty_img
        self.images[hero] = img
        return img

    def _build(self):
        s = self.scale
        self.root.overrideredirect(True)
        self.root.configure(bg=TRANSPARENT)
        try:
            self.root.attributes("-topmost", True)
            self.root.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            # older tk builds have no transparency; it still works, just with a box
            self.root.configure(bg=BG)

        self.empty_img = tk.PhotoImage(width=PORTRAIT, height=PORTRAIT)
        self.empty_img.put(EMPTY_BG, to=(0, 0, PORTRAIT, PORTRAIT))

        frame = tk.Frame(self.root, bg=TRANSPARENT)
        frame.pack(padx=int(PAD * s), pady=int(PAD * s))

        # a leading tile that says which player layout is on screen: the same
        # strip is used in both modes, and the operator needs to know at a glance
        # whether the long bars (normal) or the squares (teamfight) are on air right now
        mode = tk.Frame(frame, bg=BG, padx=int(6 * s), pady=int(2 * s),
                        highlightthickness=1, highlightbackground=BG)
        mode.grid(row=0, column=0, padx=(0, int(GAP * s / 2)))
        tk.Label(mode, text="players", bg=BG, fg=KEY_FG,
                 font=("Bahnschrift", int(9 * s))).pack(pady=(int(22 * s), 0))
        self.mode_label = tk.Label(mode, text="NORMAL", bg=BG, fg=MODE_BARS,
                                   font=("Bahnschrift", int(13 * s), "bold"))
        self.mode_label.pack(pady=(0, int(22 * s)))
        self.mode_box = mode

        for i, key in enumerate(KEYS):
            cell = tk.Frame(frame, bg=BG, padx=int(2 * s), pady=int(2 * s),
                            highlightthickness=1, highlightbackground=BG)
            cell.grid(row=0, column=i + 1, padx=int(GAP * s / 2))
            label = tk.Label(cell, text=key, bg=BG, fg=KEY_FG,
                             font=("Bahnschrift", int(13 * s), "bold"))
            label.pack()
            art = tk.Label(cell, bg=BG, image=self.empty_img, bd=0)
            art.pack(pady=(int(2 * s), int(2 * s)))
            self.cells.append({"key": key, "art": art, "img": None, "box": cell})

        self.root.update_idletasks()
        self._place()
        # dragging: a strip that cannot be moved is useless on a second monitor
        self.root.bind("<Button-1>", self._press)
        self.root.bind("<B1-Motion>", self._drag)
        self.root.bind("<Double-Button-1>", lambda e: self.root.destroy())
        for row in (frame,) + tuple(self.cells[i]["art"].master for i in range(len(self.cells))):
            row.bind("<Button-1>", self._press)
            row.bind("<B1-Motion>", self._drag)
            row.bind("<Double-Button-1>", lambda e: self.root.destroy())

    def _place(self):
        x, y, self.order = load_layout()
        if x is None:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w = self.root.winfo_reqwidth()
            h = self.root.winfo_reqheight()
            x, y = (sw - w) // 2, sh - h - 60
        self.root.geometry("+%d+%d" % (x, y))

    def _press(self, ev):
        self._drag_from = (ev.x_root - self.root.winfo_x(), ev.y_root - self.root.winfo_y())

    def _drag(self, ev):
        dx, dy = getattr(self, "_drag_from", (0, 0))
        self.root.geometry("+%d+%d" % (ev.x_root - dx, ev.y_root - dy))

    def _dropped(self, _ev=None):
        save_layout(self.root.winfo_x(), self.root.winfo_y(), self.order)

    # ---------- data ----------

    def _poll(self):
        """asks the hub for state on a worker thread; the tk thread does all ui."""
        misses = 0
        last_ok = time.time()
        while True:
            try:
                with urllib.request.urlopen(self.hub + "/api/state", timeout=2) as r:
                    state = json.loads(r.read().decode("utf-8"))
                misses = 0
                last_ok = time.time()
                self.ui.put(("state", state))
            except Exception:
                misses += 1
                if misses == 1:
                    print("keys: hub unreachable, dimming the strip", flush=True)
                self.ui.put(("stale", None))
                # a hub that is gone for good would otherwise leave this window
                # floating over the game for ever. count by wall clock, not by
                # attempts: a refused connection returns at once, a black holed
                # one can burn the whole timeout, and neither may keep us alive.
                if time.time() - last_ok >= 20.0:
                    self.ui.put(("quit", None))
                    time.sleep(0.2)
                    # leave the process, not just the window: if tk ever refuses
                    # to destroy (bad state, stuck mainloop) this still gets rid
                    # of the strip, and the hub is gone so nothing can restart it
                    try:
                        os.remove(PID_FILE)
                    except OSError:
                        pass
                    os._exit(0)
            time.sleep(0.4)

    def _drain_ui(self):
        """everything that touches tk runs here, on the tk thread."""
        try:
            while True:
                kind, value = self.ui.get_nowait()
                if kind == "state":
                    self.render(value, True)
                elif kind == "stale":
                    self.render(None, False)
                elif kind == "quit":
                    self._closing = True
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(80, self._drain_ui)

    def render(self, state, ok):
        self.ok = ok
        # a dead hub keeps the last strip on screen but dims it, so the operator
        # can tell "stale" from "wrong"
        self.root.attributes("-alpha", 1.0 if ok else 0.4)
        if not ok:
            return
        self.state = state
        cells = slots(state)
        self.order = list(self.order)[:len(cells)] or blank_order()
        for i, cell in enumerate(self.cells):
            src = cells[self.order[i]] if self.order[i] < len(cells) else {"hero": None}
            img = self._portrait(src.get("hero"))
            if cell["img"] is not img:
                cell["img"] = img
                cell["art"].configure(image=img)
        compact = bool(state.get("compact"))
        want = "TEAMFIGHT" if compact else "NORMAL"
        if self.mode_label.cget("text") != want:
            self.mode_label.configure(text=want,
                                      fg=MODE_COMPACT if compact else MODE_BARS)
            self.mode_box.configure(highlightbackground=MODE_COMPACT if compact
                                    else ARM_ON)

    def start(self):
        self.root.bind("<ButtonRelease-1>", self._dropped)
        threading.Thread(target=self._poll, daemon=True).start()
        self._keep_on_top()
        self._poll_keys()
        self._drain_ui()
        try:
            with io.open(PID_FILE, 'w', encoding='utf-8') as fh:
                fh.write(str(os.getpid()))
        except Exception:
            pass

    # ---------- swap mode ----------
    #
    # read-only key watching: GetAsyncKeyState on the tk thread, no hook, nothing
    # swallowed. 0x8000 means "held right now" and drives the edge detection.
    #
    # the 0x0001 bit ("pressed since the last call") is deliberately NOT used: we
    # only read these keys while the swap key is held, so presses made before that
    # would stay latched and fire the moment the strip armed - which looked like
    # "it swaps without me pressing anything". keys are read every tick instead,
    # so the latch is always consumed, and only the held-bit edges count.

    def _poll_keys(self):
        try:
            user32 = ctypes.windll.user32
            mod = bool(user32.GetAsyncKeyState(TRY_SWAP_VK) & 0x8000)
            if mod != self.armed:
                self._set_armed(mod)
            for i, vk in enumerate(VK_BY_CELL):
                held = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                fresh = held and not self.key_down.get(i)
                self.key_down[i] = held
                if fresh and mod:
                    self._on_cell_key(i)
        except Exception:
            # never let a polling hiccup take the strip down; just try again
            pass
        self.root.after(40, self._poll_keys)

    def _on_cell_key(self, cell):
        """one of the twelve keys was pressed while armed: pick it, then swap."""
        if not self.armed:
            return
        if not self.picks:
            self.picks = [cell]
            self._paint_armed()
        elif cell in self.picks:
            self.picks = []
            self._paint_armed()
        else:
            self._do_swap(self.picks[0], cell)

    def _set_armed(self, armed):
        self.armed = armed
        if not armed:
            self.picks = []
        self._paint_armed()

    def _paint_armed(self):
        for i, cell in enumerate(self.cells):
            hot = self.armed and i in self.picks
            cell["box"].configure(
                highlightbackground=ARM_PICK if hot else (ARM_ON if self.armed else BG),
                highlightthickness=2 if self.armed else 1)

    def _set_pick(self, cell):
        self.picks = [] if cell is None else [cell]
        self._paint_armed()

    def _do_swap(self, a, b):
        """two keys picked: their portraits trade places, and it is remembered."""
        self.order = swap_order(self.order, a, b)
        save_order(self.order)
        self.picks = []
        self._set_armed(self.armed)
        if self.state is not None:
            self.render(self.state, True)

    def bye(self):
        """leave no pid behind for the hub to chase after a normal close."""
        try:
            if os.path.isfile(PID_FILE):
                with io.open(PID_FILE, encoding="utf-8") as fh:
                    if fh.read().strip() == str(os.getpid()):
                        os.remove(PID_FILE)
        except Exception:
            pass

    def _keep_on_top(self):
        # other always-on-top windows (obs, for one) can end up above this one, so
        # re-assert topmost now and then instead of hoping the first call sticks
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        except tk.TclError:
            pass
        self.root.after(2000, self._keep_on_top)


def take_single_instance_lock(port=8791):
    """Only one strip may exist. Binds a loopback port as the lock: if it is
    taken, another strip is already up and this one should just exit.

    Without this, every hub restart during development left another window
    floating over the game, and they stacked up.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s            # keep the reference alive for the process lifetime
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description="spectate key strip")
    ap.add_argument("--hub", default="http://127.0.0.1:8787")
    ap.add_argument("--scale", type=float, default=1.0, help="scale the padding and text")
    args = ap.parse_args()

    lock = take_single_instance_lock()
    if lock is None:
        print("keys: another strip is already running, leaving this one closed", flush=True)
        return

    root = tk.Tk()
    root.title("deadlock keys")
    strip = Strip(root, args.hub, args.scale)
    strip.start()
    root.mainloop()
    # the position is already saved on drop and on swap; saving again here would
    # overwrite it with whatever tk reports while the window is being destroyed
    strip.bye()


if __name__ == "__main__":
    main()
