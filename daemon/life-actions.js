/* ABUZ8 LIFE — the gated action surface.

   The UI gates below MIRROR the server's gates; they never replace them. Every
   rule here is enforced independently in lifed.py and was tested by driving the
   API directly with the page closed — a modified page cannot widen what the
   daemon allows.

   Tier 1  local, reversible          → one confirmation (open a document)
   Tier 2  runs code / persistent     → type CONFIRM (launch an app: type RUN)
   Tier 3  speaks publicly as Ahmad   → type the exact task name, and the daemon
           must have been started with --allow-external (off by default)
   Tier 4  delete                     → triple-yes (CONFIRM → exact file name →
           DELETE + token), then quarantine, never unlink. Probes and the brain
           are read-only forever: nothing acts on its own.

   Still absent: money movement, key rotation, git reset/clean. Those bind Ahmad
   to something or destroy history across a repo — his hands, not a button. */

const ACT = { list: [], allowExternal: false, audit: [], busy: null, loaded: false };

async function loadActions() {
  try {
    const r = await fetch(LIVE.base + '/api/actions', { cache: 'no-store' });
    const d = await r.json();
    ACT.list = d.actions || [];
    ACT.allowExternal = !!d.allowExternal;
    ACT.loaded = true;
  } catch (e) { ACT.loaded = false; }
  try {
    const r = await fetch(LIVE.base + '/api/audit', { cache: 'no-store' });
    ACT.audit = (await r.json()).entries || [];
  } catch (e) {}
}

async function runAction(id, confirm) {
  ACT.busy = id;
  render();
  let out;
  try {
    // The nonce is same-origin only, so this whole path is unavailable from file://.
    const nr = await fetch(LIVE.base + '/api/nonce', { cache: 'no-store' });
    if (!nr.ok) throw new Error('actions need the app served from http://127.0.0.1:4310');
    const { nonce } = await nr.json();
    const r = await fetch(LIVE.base + '/api/act', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, confirm, nonce }),
    });
    out = await r.json();
  } catch (e) {
    out = { ok: false, detail: e.message };
  }
  ACT.busy = null;
  await loadActions();
  render();
  const el = document.getElementById('act-result');
  if (el) {
    el.className = 'strip ' + (out.ok ? 'ok' : 'bad');
    el.innerHTML = `<b>${out.ok ? 'Done' : 'Refused'} — ${esc(id)}</b><br>${esc(out.detail || '')}`;
  }
  return out;
}

function askAndRun(id, tier, title, external) {
  if (tier === 1) {
    if (!window.confirm(`${title}\n\nThis runs now. It is reversible.\nProceed?`)) return;
    return runAction(id, 'CONFIRM');
  }
  if (tier === 2) {
    const v = window.prompt(`${title}\n\nThis changes persistent configuration.\nType CONFIRM to proceed.`);
    if (v === null) return;
    return runAction(id, v.trim());
  }
  // tier 3 — the task must be named back exactly, so it cannot be done by reflex
  const name = id.split(':')[1] || '';
  const v = window.prompt(
    `${title}\n\nTHIS JOB POSTS OR SENDS PUBLICLY AS AHMAD.\n` +
    `Arming it means the machine speaks on his behalf on a schedule.\n\n` +
    `Type the task name exactly to proceed:\n${name}`);
  if (v === null) return;
  return runAction(id, v.trim());
}

const TIER_META = {
  1: { cls: 'ok',   label: 'local · reversible',        gate: 'one confirmation' },
  2: { cls: 'warn', label: 'persistent config',         gate: 'type CONFIRM' },
  3: { cls: 'bad',  label: 'speaks publicly as Ahmad',  gate: 'type the exact task name' },
  4: { cls: 'bad',  label: 'delete · triple-gated',     gate: 'CONFIRM → filename → DELETE token' },
};

async function askOpenDlg() {
  const v = window.prompt('Open a file or launch an app.\n\nMust live under E:\\ABU, Documents, or Desktop.\nDocuments open with one confirmation; executables need RUN typed back and run with no arguments.\n\nFull path:');
  if (v === null) return;
  const path = v.trim();
  if (!path) return;
  const id = 'open.file:' + path;
  if (!window.confirm(`Open now?\n\n${path}`)) return;
  let out = await runAction(id, 'CONFIRM');
  if (!out.ok && /launches code/.test(out.detail || '')) {
    const r = window.prompt(`THIS LAUNCHES CODE.\n\n${path}\n\nNo arguments are ever passed.\nType RUN to proceed.`);
    if (r === null) return;
    out = await runAction(id, r.trim());
  }
}

async function askDeleteDlg() {
  const v = window.prompt('DELETE — triple-yes gate.\n\nThe file moves to quarantine (reversible), never unlinked.\nDaemon live files (lifed.py, LIFE.html, actions.log, brain.db) are refused.\n\nFull path:');
  if (v === null) return;
  const path = v.trim();
  if (!path) return;
  const id = 'delete.file:' + path;
  if (!window.confirm(`YES #1 of 3 — you want this file GONE from its folder?\n\n${path}\n\nIt goes to quarantine, not the void.`)) return;
  let out = await runAction(id, 'CONFIRM');
  if (!out.ok) return;
  const m2 = /STEP2 fp=(\S+) name=(.*) size=(\d+)/.exec(out.detail || '');
  if (!m2) return;
  const name = window.prompt(`YES #2 of 3 — type the EXACT file name:\n\n${m2[2]}`);
  if (name === null) return;
  out = await runAction(id, name.trim());
  if (!out.ok) return;
  const m3 = /STEP3 token=(\S+)/.exec(out.detail || '');
  if (!m3) return;
  const fin = window.prompt(`YES #3 of 3 — point of no return (still reversible via quarantine).\n\nType:\nDELETE ${m3[1]}`);
  if (fin === null) return;
  await runAction(id, fin.trim());
}

function sOpenDelete() {
  return `<div class="sect"><h2>Open · Delete</h2>
    <span class="note">wide surface · gates match blast radius · probes never act on their own</span></div>
  <div class="tiles">
    <div class="tile" style="min-height:auto;cursor:default">
      <div class="nm">Open a file / launch an app</div>
      <div class="mt" style="white-space:normal;line-height:1.5">Documents open with one confirmation. Executables need RUN typed back, run with no arguments, under the managed roots.</div>
      <button class="btn" onclick="askOpenDlg()">Open…</button>
    </div>
    <div class="tile" style="min-height:auto;cursor:default">
      <div class="nm">Delete a file <span class="chip c-dead">T4</span></div>
      <div class="mt" style="white-space:normal;line-height:1.5">Triple-yes: CONFIRM → exact file name → DELETE + token. Quarantined, never unlinked. Daemon live files refused.</div>
      <button class="btn gold" onclick="askDeleteDlg()">Delete…</button>
    </div>
  </div>`;
}

function sActions() {
  if (!LIVE.on) {
    return `<div class="strip warn"><b>Actions are unavailable right now.</b> They require the app to be
      served by the daemon at <code>http://127.0.0.1:4310</code> — a copy opened straight off disk can read
      everything but can never act, which is deliberate. Run
      <code>E:\\ABU\\LIFE\\start-live.cmd</code> and open the served page.</div>`;
  }
  if (!ACT.loaded) { loadActions().then(render); return `<div class="empty">Loading the action catalog…</div>`; }

  const byTier = { 1: [], 2: [], 3: [], 4: [] };
  ACT.list.forEach(a => (byTier[a.tier] || byTier[1]).push(a));

  const card = a => {
    const m = TIER_META[a.tier];
    const locked = !a.available;
    const busy = ACT.busy === a.id;
    return `<div class="tile" style="min-height:auto;cursor:default;${locked ? 'opacity:.6' : ''}">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:start">
        <div class="nm">${esc(a.title)}</div>
        <span class="chip c-${m.cls === 'ok' ? 'live' : (m.cls === 'warn' ? 'wip' : 'dead')}">T${a.tier}</span>
      </div>
      <div class="mt" style="white-space:normal;line-height:1.5">${esc(a.why)}</div>
      ${a.undo ? `<div class="mt" style="white-space:normal;color:var(--ink-3)">Undo: ${esc(a.undo)}</div>` : ''}
      ${locked
        ? `<div class="mt" style="color:var(--bad)">LOCKED — ${esc(a.needs || 'unavailable')}</div>`
        : `<button class="btn ${a.tier === 1 ? '' : 'gold'}" ${busy ? 'disabled' : ''}
             onclick="askAndRun('${esc(a.id).replace(/'/g, "\\'")}',${a.tier},'${esc(a.title).replace(/'/g, "\\'")}',${!!a.external})">
             ${busy ? 'working…' : 'Run'}${a.slow ? ' (slow)' : ''}</button>`}
    </div>`;
  };

  return `<div id="act-result" class="strip info"><b>Wide surface, gated by blast radius.</b>
    The page sends an action ID, never a command — no free text ever reaches a shell. Every request is
    same-origin, carries a single-use nonce, and is appended to <code>actions.log</code> whether it
    succeeds or is refused. Delete exists behind a triple-yes quarantine gate; probes never act on their own.</div>

    ${!ACT.allowExternal ? `<div class="strip bad"><b>External-posting jobs are locked.</b>
      Six scheduled jobs post or send publicly as Ahmad. They are visible below but cannot be armed unless
      the daemon is started with <code>--allow-external</code>, which is off by default. That switch is a
      deliberate speed bump, not a formality — arming one makes this machine speak as him on a schedule.</div>` : `
      <div class="strip warn"><b>--allow-external is ON.</b> Jobs that post publicly as Ahmad can be armed
      from this page. Each still requires its exact name typed back.</div>`}

    ${[1, 2, 3, 4].map(t => byTier[t].length ? `
      <div class="sect"><h2>Tier ${t}</h2>
        <span class="note">${TIER_META[t].label} · gate: ${TIER_META[t].gate}</span>
        <span class="ct">${byTier[t].length}</span></div>
      <div class="tiles">${byTier[t].map(card).join('')}</div>` : '').join('')}

    <div class="sect"><h2>Audit</h2>
      <span class="note">Every attempt, allowed or refused, with the reason</span>
      <span class="ct">${ACT.audit.length}</span></div>
    ${ACT.audit.length ? `<div class="tw"><table>
      <thead><tr><th class="nos">When</th><th class="nos">Action</th><th class="nos">Result</th><th class="nos">Detail</th></tr></thead>
      <tbody>${ACT.audit.slice().reverse().map(e => `<tr>
        <td class="m">${esc((e.at || '').replace('T', ' ').slice(0, 19))}</td>
        <td class="m">${esc(e.id || '?')}</td>
        <td><span class="chip c-${e.ok ? 'live' : 'dead'}">${e.ok ? 'ran' : 'refused'}</span></td>
        <td>${esc((e.detail || '').slice(0, 130))}</td>
      </tr>`).join('')}</tbody></table></div>` : '<div class="empty">No actions attempted yet.</div>'}

    ${sOpenDelete()}

    <div class="panel">
      <h3>What stays out of reach</h3>
      <p class="sub">Delete runs only through the triple-yes quarantine gate above — nothing acts
      on its own, ever. Still absent by doctrine: money movement, key rotation, and
      <code>git reset</code> / <code>git clean</code>. Those bind Ahmad to something or destroy
      history across a repo — his hands, not a button.</p>
    </div>`;
}
