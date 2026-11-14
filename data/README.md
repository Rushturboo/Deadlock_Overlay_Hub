# data/

Everything in this folder is **generated**, not source:

| folder / file | what it is | where it comes from |
|---|---|---|
| `portraits/<hero_id>.png` | hero faces for the player cards, the kill feed and the keys strip | `GET /v1/assets/heroes` (images field) |
| `icons/<item_id>.png` | shop item icons for the 12 item slots | `GET /v1/assets/items` (shop_image) |
| `items.json` | the item table: names, `is_active_item`, base cooldowns, icon urls | same endpoint |
| `heroes.json` | the hero table: names, portrait urls | the heroes endpoint |
| `map/map.json`, `map/mid.png`, `map/frame.png` | the minimap plate | `GET /v1/assets/map` |
| `keys-window.json` | where you dragged the keys strip, and your key→player swaps | this machine |
| `keys.pid` | pid of the running strip, so the hub can close it | this machine |

The hub fills the art files in by itself: the first start downloads them in the
background (a minute or so on a slow line), and the widgets just show a placeholder
until each file lands. If you want to look at the widgets before that, run the hub once
with the game closed and wait for the `items:` / `heroes:` lines in its console.

So none of it is committed, for two reasons: it is regenerable, and the art belongs to
Valve - only the code is mine to publish.
