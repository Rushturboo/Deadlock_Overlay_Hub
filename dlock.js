/* deadlock broadcast overlay shared library - v2
 *
 * two modes, same state shape:
 *   hub     : pages are served by hub.py and poll /api/state every 400ms
 *             (the hub is the only source; ?demo=1 swaps in fake data)
 *
 * state = {
 *   phase, matchId, clock, title, series:{bo,a,b}, error,
 *   teams:[{id,name,logo,score,netWorth,kills,structuresDown,structuresTotal,
 *           structuresKnown}],
 *   players:[{ctrl,pawn,name,team,hero,heroName,level,gold,netWorth,
 *             kills,deaths,assists,alive,hp,hpMax,verified}],
 *   kills:[{id,attacker,attackerTeam,victim,victimTeam,gold,firstBlood,ageMs}]
 * }
 */
const DL = (() => {
  const params = new URLSearchParams(location.search);
  const CFG = {
    t0: params.get('t0') || 'Hidden King',
    t1: params.get('t1') || 'Arch Mother',
    // direct mode has no panel, so the series stays at its default there
    bo: Number(params.get('bo')) || 3,
    s0: params.get('s0') || '',
    s1: params.get('s1') || '',
    demo: params.get('demo') === '1',
    hub: params.get('hub') === '1',
    poll: params.get('poll') === '1',
    compact: params.get('compact') === '1',
  };

  const HERO = {
    1: 'Infernus', 2: 'Seven', 3: 'Vindicta', 4: 'Lady Geist', 6: 'Abrams',
    7: 'Wraith', 8: 'McGinnis', 10: 'Paradox', 11: 'Dynamo', 12: 'Kelvin',
    13: 'Haze', 14: 'Holliday', 15: 'Bebop', 16: 'Calico', 17: 'Grey Talon',
    18: 'Mo & Krill', 19: 'Shiv', 20: 'Ivy', 21: 'Kali', 25: 'Warden',
    27: 'Yamato', 31: 'Lash', 35: 'Viscous', 38: 'Gunslinger', 39: 'The Boss',
    48: 'Wrecker', 49: 'Rutger', 50: 'Pocket', 51: 'Thumper', 52: 'Mirage',
    53: 'Fathom', 54: 'Cadence', 56: 'Bomber', 57: 'Shield Guy', 58: 'Vyper',
    59: 'Vandal', 60: 'Sinclair', 61: 'Trapper', 62: 'Raven', 63: 'Mina',
    64: 'Drifter', 65: 'Venator', 66: 'Victor', 67: 'Paige', 68: 'Boho',
    69: 'The Doorman', 70: 'Skyrunner', 71: 'Swan', 72: 'Billy', 73: 'Druid',
    74: 'Graf', 75: 'Fortuna', 76: 'Graves', 77: 'Apollo', 78: 'Deadman Danny',
    79: 'Rem', 80: 'Silver', 81: 'Celeste', 82: 'Opera', 84: 'Rat King',
    85: 'Solomon', 86: 'Violet', 87: 'Nurse Harrow', 88: 'Baba',
  };

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const handleToIndex = (h) => {
    const n = Number(h);
    return Number.isFinite(n) ? ((n % 16384) + 16384) % 16384 : null;
  };
  const teamClass = (t) => (t === 2 ? 'amber' : t === 3 ? 'sapphire' : 'neutral');
  const teamName = (t) => (t === 2 ? CFG.t0 : t === 3 ? CFG.t1 : 'NEUTRAL');
  const fmtGold = (v) => {
    const n = Number(v) || 0;
    return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(Math.round(n));
  };
  const fmtClock = (sec) => {
    if (sec == null || !Number.isFinite(sec)) return '--:--';
    const s = Math.max(0, Math.floor(sec));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  };


  // ---------------------------------------------------------------- plumbing
  let pending = false;
  let render = () => {};
  const changed = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; render(); });
  };

  const useHub = () => true;   // the hub is the only data source

  function demoState() {
    const players = [];
    const names = ['Rework Mirage Yoshi', 'pyon', 'PaperAbyss', 'BULKENSTEIN', 'Saiff', 'black forest ham',
      '02', 'Kanaka', 'Rug', 'cj', 'Fiery_Eggroll', 'fy'];
    const heroes = [78, 85, 73, 65, 73, 31, 13, 63, 17, 2, 7, 59];
    for (let i = 0; i < 12; i++) {
      const team = i < 6 ? 2 : 3;
      players.push({
        ctrl: i + 1, pawn: 76 + i, name: names[i], team, hero: heroes[i],
        heroName: HERO[heroes[i]] || '?', level: 6 + i, gold: 3000 + i * 400,
        netWorth: 3700 + i * 180, kills: i, deaths: 3 - (i % 4), assists: i % 5,
        alive: i !== 2 && i !== 8, hp: 400 + i * 70, hpMax: 900 + i * 20, verified: true,
        // a couple of players carry cooldowns so `?demo=1` shows the fan
        cd: i === 1 ? {
          ult: { trained: true, remain: 42.0, total: 90.0, ready: false },
          items: [{ key: 'CCitadel_Item_MetalSkin', id: 668299740, remain: 12.0, total: 30.0 }],
        } : i === 4 ? {
          ult: { trained: true, remain: 0, total: 90.0, ready: true },
          items: [
            { key: 'CCitadel_Item_MetalSkin', id: 7409189, remain: 0, total: 30.0 },
            { key: 'CCitadel_Item_DebuffRemover', id: 2829638276, remain: 3.0, total: 24.0 },
          ],
        } : { ult: { trained: true, remain: 0, total: 90.0, ready: true }, items: [] },
      });
    }
    return {
      phase: 'live', matchId: 'demo', clock: 423.5, error: null,
      title: 'GRAND FINAL',
      // players.html reads this to pick the square + ability layout; the hub sends
      // the real one, demo mode takes `?compact=1` so both shapes can be shot
      compact: CFG.compact,
      series: { bo: 3, a: 1, b: 0 },
      teams: [
        { id: 2, name: CFG.t0, score: 4, netWorth: 24500, kills: 17,
          structuresDown: 4, structuresTotal: 12, structuresKnown: true,
          logo: 'logos/hidden-king.png' },
        { id: 3, name: CFG.t1, score: 1, netWorth: 29400, kills: 9,
          structuresDown: 2, structuresTotal: 12, structuresKnown: true,
          logo: '' },
      ],
      players,
      kills: [
        { id: 'd1', attacker: 'Fiery_Eggroll', attackerTeam: 3, victim: '02', victimTeam: 3, gold: 610, firstBlood: true, ageMs: 1200 },
        { id: 'd2', attacker: 'BULKENSTEIN', attackerTeam: 2, victim: 'Rug', victimTeam: 3, gold: 420, firstBlood: false, ageMs: 4200 },
      ],
    };
  }

  // a blank canvas tells nobody anything: say what went wrong in the page itself
  function hint(msg) {
    let el = document.getElementById('dl-hint');
    if (!el) {
      el = document.createElement('div');
      el.id = 'dl-hint';
      el.style.cssText = 'position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);' +
        'max-width:820px;padding:18px 22px;background:#11161c;color:#e8edf2;' +
        'border:1px solid #3a4652;border-radius:10px;font:14px/1.6 Inter,system-ui,sans-serif;' +
        'text-align:center;z-index:2147483647';
      document.body.appendChild(el);
    }
    el.textContent = msg;
  }
  function clearHint() {
    const el = document.getElementById('dl-hint');
    if (el) el.remove();
  }

  function start(fn) {
    // ?demo=1 must win over the hub check, otherwise a page served from the hub
    // goes looking for a live state that never arrives and stays on its skeleton
    render = () => fn(CFG.demo ? demoState() : lastHubState);
    if (CFG.demo) {
      render();
      return;
    }
    if (location.protocol.indexOf('http') !== 0) {
      hint('this page was opened straight from disk, so it has no data source.\n' +
        'open it through the hub instead: http://127.0.0.1:8787/' + location.pathname.split('/').pop() +
        '  (start-mem.cmd does this for you).');
      return;
    }
    {
      let pollTimer = null;
      let fails = 0;
      const applyState = (s) => {
        if (!s) return;
        // an edit to the widget files bumps the hub's stamp: reload once so OBS
        // picks the new markup up without a manual "refresh cache"
        if (s.stamp && lastStamp && s.stamp !== lastStamp) {
          location.reload();
          return;
        }
        if (s.stamp) lastStamp = s.stamp;
        fails = 0;
        lastHubState = s;
        render();
        clearHint();
        document.body.classList.remove('offline');
      };
      const poll = async () => {
        try {
          const r = await fetch('/api/state', { cache: 'no-store' });
          if (r.ok) applyState(await r.json());
          else document.body.classList.add('offline');
        } catch (e) { document.body.classList.add('offline'); }
        fails += 1;
        if (fails > 40) {
          hint('cannot reach the hub at ' + location.origin + '.\n' +
            'is the black hub window still open? start it with start-hub.cmd, then reload this source (right click > refresh).');
        }
        pollTimer = setTimeout(poll, 150);
      };
      const startPolling = () => { if (!pollTimer) poll(); };
      // ?poll=1 forces the polling path: a headless screenshot never finishes
      // while an EventSource connection stays open.
      if (CFG.poll) { startPolling(); return; }
      // the hub pushes at 20Hz; polling is only the fallback for a browser
      // without EventSource or a hub that closed the stream.
      try {
        const es = new EventSource('/api/stream');
        es.onmessage = (ev) => {
          let s;
          try { s = JSON.parse(ev.data); } catch (e) { return; }
          applyState(s);
        };
        es.onerror = () => {
          es.close();
          document.body.classList.add('offline');
          startPolling();
        };
      } catch (e) { startPolling(); }
      return;
    }
    render();
  }

  let lastHubState = null;
  let lastStamp = null;      // widget file stamp, see applyState in start()

  // world position -> percentage on the minimap plate. the plate is square and
  // covers [-radius, +radius] on both axes, y pointing up in the game and down
  // in css. verified against the 51 neutral camps in /v1/assets/map: residual 0.
  function mapPct(x, y, radius) {
    const r = (radius || 10752) * 2;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    const left = 50 + (x / r) * 100;
    const top = 50 - (y / r) * 100;
    if (!Number.isFinite(left) || !Number.isFinite(top)) return null;
    return { left, top };
  }

  return {
    CFG, HERO, esc, handleToIndex, teamClass, teamName, fmtGold, fmtClock,
    demoState, start, useHub, changed, mapPct,
  };
})();
