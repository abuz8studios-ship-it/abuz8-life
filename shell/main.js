// ABUZ8 LIFE — Electron shell (replaces the Tauri shell retired 2026-09-26).
// Fixes the three Tauri defects:
//  1. Tauri bundled a FROZEN copy of LIFE.html. Here the window loads the live daemon at
//     http://127.0.0.1:4310/ — same origin as /api/*, so the action gates (Sec-Fetch-Site) pass
//     and the page is always the one on disk.
//  2. Tauri spawned a new daemon without clearing a stale one on :4310. Here any stale lifed.py
//     owner of the port is killed first, so the running daemon always matches the code on disk.
//  3. Tauri killed only the python parent. Here the whole process tree is killed (taskkill /T /F),
//     on quit, on crash, and on the next launch (stale-owner sweep covers force-kill orphans).
const { app, BrowserWindow, Tray, Menu, nativeImage, shell } = require('electron');
const { spawn, execFile, execFileSync } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');

const LIFE_DIR = process.env.ABUZ8_LIFE_DIR || 'E:\\ABU\\life';
const LIFED = path.join(LIFE_DIR, 'lifed.py');
const PORT = 4310;
const ORIGIN = `http://127.0.0.1:${PORT}`;
const HIDDEN = process.argv.includes('--hidden');
const ICON = path.join(__dirname, 'assets', 'icon.png');

let win = null, tray = null, child = null, quitting = false, restarts = 0;
let logFile = null;
const log = (m) => { try { fs.appendFileSync(logFile, `${new Date().toISOString()} ${m}\n`); } catch (_) {} };

const get = (p, ms = 1500) => new Promise((res) => {
  const r = http.get(ORIGIN + p, { timeout: ms }, (x) => { x.resume(); res(x.statusCode); });
  r.on('timeout', () => { r.destroy(); res(0); });
  r.on('error', () => res(0));
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitHealthy(maxMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) { if ((await get('/api/health')) === 200) return true; await sleep(400); }
  return false;
}

const PS_SWEEP = `
$c = Get-NetTCPConnection -LocalPort ${PORT} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if(-not $c){ 'FREE'; exit }
$p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
if($p.CommandLine -match 'lifed\\.py'){ taskkill /PID $($c.OwningProcess) /T /F | Out-Null; 'KILLED' } else { 'BUSY:' + $p.Name }`;
const sweepPort = () => new Promise((res) => execFile('powershell.exe',
  ['-NoProfile', '-NonInteractive', '-Command', PS_SWEEP], { timeout: 15000, windowsHide: true },
  (e, out) => { const o = (out || '').trim(); log('sweep ' + o + (e ? ' err=' + e.message : '')); res(o || 'ERR'); }));

function killTree(pid) { try { execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }); } catch (_) {} }
function killDaemon() { if (child && child.pid) { killTree(child.pid); } child = null; }

function launch(cmd, args) {
  const out = fs.openSync(path.join(app.getPath('userData'), 'lifed.out.log'), 'a');
  const err = fs.openSync(path.join(app.getPath('userData'), 'lifed.err.log'), 'a');
  const c = spawn(cmd, args, { cwd: LIFE_DIR, windowsHide: true, stdio: ['ignore', out, err] });
  c.on('error', (e) => log(`spawn ${cmd} failed: ${e.message}`));
  c.on('exit', (code) => {
    log(`lifed exit ${code}` + (quitting ? ' (during quit — expected taskkill)' : ' UNEXPECTED'));
    if (child === c) child = null;
    if (!quitting && restarts < 3) { restarts++; log('daemon died; restart #' + restarts); setTimeout(() => startDaemon(), 1500 * restarts); }
    else if (!quitting) { log('daemon died; restart budget spent — window keeps retrying'); }
  });
  return c;
}

async function startDaemon() {
  const s = await sweepPort();
  if (s.startsWith('BUSY:')) { log('port ' + PORT + ' owned by foreign process ' + s); return false; }
  for (const [cmd, args] of [['python', ['-u', LIFED]], ['py', ['-3', '-u', LIFED]]]) {
    child = launch(cmd, args);
    if (await waitHealthy(20000)) { log(`daemon up via ${cmd} pid=${child && child.pid}`); return true; }
    killDaemon();
  }
  return false;
}

function showWin() { if (!win) return; if (win.isMinimized()) win.restore(); win.show(); win.focus(); }

function makeWindow() {
  win = new BrowserWindow({
    width: 1400, height: 900, minWidth: 900, minHeight: 600, show: false,
    backgroundColor: '#0F1318', title: 'ABUZ8 LIFE', icon: ICON, autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, spellcheck: false },
  });
  win.once('ready-to-show', () => { if (!HIDDEN) win.show(); });
  win.webContents.setWindowOpenHandler(({ url }) => { if (!url.startsWith(ORIGIN)) shell.openExternal(url); return { action: 'deny' }; });
  win.webContents.on('will-navigate', (e, url) => { if (!url.startsWith(ORIGIN)) { e.preventDefault(); shell.openExternal(url); } });
  let retries = 0;
  win.webContents.on('did-fail-load', (_e, code, desc, _u, isMain) => {
    if (!isMain || quitting) return;
    log(`did-fail-load ${code} ${desc}`);
    if (retries++ < 20) setTimeout(() => win && win.loadURL(ORIGIN + '/'), 1500);
  });
  win.on('close', (e) => { if (!quitting) { e.preventDefault(); win.hide(); } });
  win.loadURL(ORIGIN + '/');
}

function makeTray() {
  tray = new Tray(nativeImage.createFromPath(ICON));
  tray.setToolTip('ABUZ8 LIFE');
  const menu = () => Menu.buildFromTemplate([
    { label: 'Open ABUZ8 LIFE', click: showWin },
    { label: 'Reload', click: () => win && win.loadURL(ORIGIN + '/') },
    { label: 'Restart daemon', click: async () => { restarts = 0; killDaemon(); await startDaemon(); win && win.loadURL(ORIGIN + '/'); } },
    { label: 'Open logs folder', click: () => shell.openPath(app.getPath('userData')) },
    { label: 'Start on boot', type: 'checkbox', checked: app.getLoginItemSettings().openAtLogin,
      click: (i) => app.setLoginItemSettings({ openAtLogin: i.checked, args: ['--hidden'] }) },
    { type: 'separator' },
    { label: 'Quit ABUZ8 LIFE', click: () => { quitting = true; killDaemon(); app.exit(0); } },
  ]);
  tray.setContextMenu(menu());
  tray.on('click', showWin);
  tray.on('right-click', () => tray.setContextMenu(menu()));
}

if (!app.requestSingleInstanceLock()) { app.exit(0); }
else {
  app.on('second-instance', showWin);
  app.setAppUserModelId('com.abuz8.life');
  app.whenReady().then(async () => {
    logFile = path.join(app.getPath('userData'), 'shell.log');
    log('shell start; hidden=' + HIDDEN);
    makeTray();
    const ok = await startDaemon();
    if (!ok) log('DAEMON NOT HEALTHY after start — window will keep retrying');
    makeWindow();
  });
  app.on('before-quit', () => { quitting = true; killDaemon(); });
  app.on('window-all-closed', (e) => e.preventDefault());
  process.on('exit', () => { if (child && child.pid) killTree(child.pid); });
}
