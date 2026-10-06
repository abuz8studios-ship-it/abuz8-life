"""ABUZ8 LIFE — probe daemon.

A browser page cannot run nvidia-smi, open a socket, or read a git tree. This
daemon does the probing and serves the result as JSON so LIFE polls live state
instead of carrying a snapshot.

Design rules, in order of importance:

  1. READ-ONLY. There is no endpoint that writes, deletes, starts, or stops
     anything. This is an instrument, not a control panel. Anything that could
     mutate the machine stays with Ahmad.
  2. LOCALHOST ONLY. It binds 127.0.0.1. The payload contains secret file paths
     and full system state; it must never be reachable from the network.
  3. NEVER BLOCK. Probes run on background threads at tiered cadences and the
     HTTP handler only ever serves the last known value. A hung git cannot wedge
     a request.
  4. HONEST STALENESS. Every probe group carries its own `at` and `age_s`, so the
     UI can say how old each number is rather than implying everything is fresh.

Run:  python E:\\ABU\\LIFE\\lifed.py
Then: http://127.0.0.1:4310
"""

import json, os, re, sqlite3, subprocess, sys, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 4310
ROOT = os.path.dirname(os.path.abspath(__file__))
APP  = os.path.join(ROOT, "LIFE.html")
BRAIN = os.path.join(ROOT, "living-brain.html")
VENDOR = os.path.join(ROOT, "vendor")
ABU  = r"E:\ABU"

# Windows: keep console windows from flashing on every probe.
NOWIN = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

# CREATE_NO_WINDOW is not honored consistently for every console executable
# (especially the inbox Windows PowerShell host). Provide STARTUPINFO as well
# so every probe stays invisible when this daemon runs at logon.
def _hidden_startupinfo():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


def run(args, timeout=8, shell=False):
    """Run a read-only command. Never raises — a failed probe returns '' and the
    previous value stays in place rather than blanking the UI.

    Probes only care about stdout. Actions must NOT use this: see run_checked."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           shell=shell, creationflags=NOWIN,
                           startupinfo=_hidden_startupinfo())
        return p.stdout.strip()
    except Exception:
        return ""


def run_checked(args, timeout=30):
    """Run and report honestly whether it worked.

    This exists because the first cut used run() for actions, which throws away
    stderr and the exit code. Register-ScheduledTask failed with 'Access is denied'
    and the action still reported 'registered' — an action layer that reports
    success on failure is worse than having no action layer at all.

    PowerShell adds a wrinkle: a failing cmdlet often still exits 0 while writing
    to stderr, so a non-empty stderr is treated as failure too."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=NOWIN, startupinfo=_hidden_startupinfo())
    except subprocess.TimeoutExpired:
        return False, "timed out after %ss" % timeout
    except Exception as e:
        return False, "could not launch: %s" % e
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0:
        return False, (err or out or "exit code %d" % p.returncode)[-600:]
    if err:
        return False, err[-600:]
    return True, (out or "completed")[-600:]


def ps(script, timeout=25):
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout)


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ── probes ──────────────────────────────────────────────────────────────
def probe_gpu():
    out = run(["nvidia-smi",
               "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
               "--format=csv,noheader,nounits"], timeout=8)
    gpus = []
    for line in out.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) >= 7:
            try:
                gpus.append({"i": int(f[0]), "name": f[1], "totalMB": int(f[2]),
                             "usedMB": int(f[3]), "freeMB": int(f[4]),
                             "util": int(f[5]), "tempC": int(f[6])})
            except ValueError:
                pass
    return gpus


# The ports LIFE knows about. A closed port here is a measured absence.
PORT_MAP = [
    (8011,  "Ornith 1.0 35B — ops-launched brain"),
    (8902,  "Ornith 35B — ABUZ8 OS bundled brain"),
    (18789, "Zait / OpenClaw gateway"),
    (8900,  "QADIR_CORE FastAPI — the living memory brain"),
    (8188,  "ComfyUI — render pipeline"),
    (11434, "Ollama"),
    (4000,  "LiteLLM router"),
    (4100,  "Fleet dashboard"),
    (8013,  "LFM2.5 fast lane"),
    (8014,  "AgentWorld fast lane"),
    (8007,  "OmniBrain sprinter"),
    (8099,  "GLM-5.2 brain"),
    (8500,  "OSGLM fused API"),
    (7734,  "Legacy bridge"),
    (8001,  "Nemotron 30B brain — ops-launched"),
    (8360,  "LotVue / 360Car — LIVE Stripe+Gmail, binds 0.0.0.0"),
    (8090,  "Qwen3-8B Q8 — QADIR embedding brain"),
    (8092,  "LFM2.5-2.6B — LIFE ISNAD brain (1.56 GB, Q4_K_M)"),
]


def probe_dynamic():
    """Discover services that do NOT sit on a fixed port.

    ABUZ8 OS binds an ephemeral port on purpose (auto_port), and its bundled
    llama-server does not use the ops :8011. A hardcoded map therefore reports a
    perfectly healthy system as almost entirely dead — which is exactly what
    happened: 12 of 14 'down' while the OS was serving happily on :60151.

    The standing doctrine is to find it by PROCESS OWNERSHIP, never a fixed curl
    and never the .port file (which has been observed lying). That is what this does."""
    out = ps(
        "$names = 'ABUZ8 OS','llama-server','ollama','ComfyUI';"
        "$procs = Get-Process -EA SilentlyContinue | ? { $names -contains $_.ProcessName };"
        "if ($procs) {"
        "  $ids = $procs.Id;"
        "  Get-NetTCPConnection -State Listen -EA SilentlyContinue |"
        "   ? { $ids -contains $_.OwningProcess } |"
        "   Select-Object -Unique LocalPort,OwningProcess |"
        "   % { $p = Get-Process -Id $_.OwningProcess -EA SilentlyContinue;"
        "       '{0}|{1}|{2}' -f $_.LocalPort,$_.OwningProcess,$p.ProcessName } }",
        timeout=20)
    rows = []
    for line in out.splitlines():
        f = line.split("|")
        if len(f) >= 3 and f[0].isdigit():
            rows.append({"p": int(f[0]), "pid": int(f[1]) if f[1].isdigit() else None,
                         "proc": f[2], "discovered": True})
    return rows


def probe_ports():
    """One PowerShell call for all ports — 14 separate calls would cost seconds."""
    want = ",".join(str(p) for p, _ in PORT_MAP)
    out = ps(
        "$w=@(" + want + ");"
        "Get-NetTCPConnection -State Listen -EA SilentlyContinue |"
        " ? { $w -contains $_.LocalPort } |"
        " Select-Object -Unique LocalPort,OwningProcess |"
        " % { $p=Get-Process -Id $_.OwningProcess -EA SilentlyContinue;"
        "     '{0},{1},{2}' -f $_.LocalPort,$_.OwningProcess,$p.ProcessName }",
        timeout=20)
    live = {}
    for line in out.splitlines():
        f = line.split(",")
        if len(f) >= 2 and f[0].isdigit():
            live[int(f[0])] = {"pid": int(f[1]) if f[1].isdigit() else None,
                               "proc": f[2] if len(f) > 2 else ""}
    rows = []
    for p, role in PORT_MAP:
        hit = live.get(p)
        rows.append({"p": p, "role": role, "open": bool(hit),
                     "pid": hit["pid"] if hit else None,
                     "proc": hit["proc"] if hit else None})
    # Append anything discovered by process ownership that the fixed map missed,
    # so a service on an ephemeral port is never reported as absent.
    known = {p for p, _ in PORT_MAP}
    for d in probe_dynamic():
        if d["p"] in known:
            continue
        rows.append({"p": d["p"], "open": True, "pid": d["pid"], "proc": d["proc"],
                     "discovered": True,
                     "role": "%s — discovered by process, not a fixed port" % d["proc"]})
    return rows


# PowerShell's N1/N2 format inserts a thousands separator — "1,862.0" — which
# silently shreds any comma-delimited parse. Every probe below therefore uses a
# pipe delimiter and [math]::Round, never -f with an N format.
def probe_procs():
    out = ps(
        "Get-Process | ? { $_.ProcessName -match 'llama|ollama|python|node|electron|comfy|pwsh' } |"
        " Group-Object ProcessName | % { '{0}|{1}|{2}' -f $_.Name,$_.Count,"
        "[math]::Round((($_.Group | Measure-Object WorkingSet64 -Sum).Sum/1GB),2) }", timeout=20)
    rows = []
    for line in out.splitlines():
        f = line.split("|")
        if len(f) >= 3:
            try:
                rows.append({"name": f[0], "count": int(f[1]), "gb": float(f[2])})
            except ValueError:
                pass
    return sorted(rows, key=lambda r: -r["gb"])


def probe_host():
    out = ps("$o=Get-CimInstance Win32_OperatingSystem;"
             "'{0}|{1}|{2}' -f [math]::Round($o.TotalVisibleMemorySize/1MB,1),"
             "[math]::Round($o.FreePhysicalMemory/1MB,1),"
             "$o.LastBootUpTime.ToString('yyyy-MM-dd HH:mm')", timeout=15)
    f = out.split("|")
    if len(f) >= 3:
        try:
            tot, free = float(f[0]), float(f[1])
            boot = datetime.strptime(f[2], "%Y-%m-%d %H:%M")
            up = datetime.now() - boot
            return {"ramTotalGB": round(tot, 1), "ramFreeGB": round(free, 1),
                    "bootedAt": f[2],
                    "uptime": "%dh %02dm" % (up.total_seconds() // 3600, (up.total_seconds() % 3600) // 60)}
        except Exception:
            pass
    return None


def probe_volumes():
    out = ps("Get-Volume | ? DriveLetter | % { '{0}|{1}|{2}|{3}' -f "
             "$_.DriveLetter,$_.FileSystemLabel,[math]::Round($_.Size/1GB,1),"
             "[math]::Round($_.SizeRemaining/1GB,1) }", timeout=20)
    rows = []
    for line in out.splitlines():
        f = line.split("|")
        if len(f) >= 4:
            try:
                rows.append({"d": f[0], "label": f[1],
                             "totalGB": float(f[2]), "freeGB": float(f[3])})
            except ValueError:
                pass
    return rows


def probe_repos():
    """Slow tier — 19 repos × 4 git calls. Timeboxed per repo."""
    rows = []
    try:
        names = sorted(os.listdir(ABU))
    except OSError:
        return rows
    for n in names:
        d = os.path.join(ABU, n)
        if not os.path.isdir(os.path.join(d, ".git")):
            continue
        g = ["git", "-C", d]
        br = run(g + ["rev-parse", "--abbrev-ref", "HEAD"], timeout=6)
        if not br:
            continue
        sha = run(g + ["rev-parse", "--short", "HEAD"], timeout=6)
        st = run(g + ["status", "--porcelain"], timeout=20)
        last = run(g + ["log", "-1", "--format=%cs"], timeout=6)
        rows.append({"name": n, "branch": br, "sha": sha,
                     "dirty": len([x for x in st.splitlines() if x.strip()]),
                     "last": last})
    return sorted(rows, key=lambda r: -r["dirty"])


SECRET_PATHS = [
    (r"E:\ABU\00_INBOX\2026-06-11_desktop\stripe keys.txt", "Stripe keys", "critical"),
    (r"E:\ABU\_SITE_INTERNAL_NODEPLOY\_stripe_audit.json", "Stripe audit dump", "critical"),
    (r"E:\ABU\_SITE_INTERNAL_NODEPLOY\_stripe_migration.json", "Stripe migration", "critical"),
    (r"E:\ABU\postiz-docker-compose\.env", "Postiz env", "high"),
    (r"E:\ABU\ABUZ8_SITE_V3\abuz8ai\.env.local", "Site env", "high"),
    (r"E:\ABU\postiz\.env", "Postiz env", "high"),
    # Found 2026-07-29 by auditing the LOTVUE Watchdog target, not by the path sweep —
    # a LIVE sk_live_ Stripe secret, whsec_ webhook secret, and a Gmail app password.
    # Correctly gitignored and untracked, but plaintext on disk, and the Watchdog task
    # would load all of it into env and bind a public app on 0.0.0.0:8360.
    (r"E:\ABU\360Car\app\.env", "LotVue LIVE Stripe + Gmail SMTP", "critical"),
]


def probe_secrets():
    """Presence check only. The daemon never opens or transmits the contents."""
    rows = []
    for path, kind, sev in SECRET_PATHS:
        try:
            st = os.stat(path)
        except OSError:
            continue
        rows.append({"path": path, "kind": kind, "sev": sev, "bytes": st.st_size,
                     "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")})
    return rows


def probe_schedule():
    out = ps("Get-ScheduledTask -EA SilentlyContinue | ? { $_.TaskName -match "
             "'ABUZ8|hermes|QEB|Brief|X_AGENT|Watchdog|comfy' } | % { $i=$_|Get-ScheduledTaskInfo -EA SilentlyContinue;"
             " '{0}|{1}|{2}' -f $_.TaskName,$_.State,"
             "$(if($i.LastRunTime){$i.LastRunTime.ToString('yyyy-MM-dd HH:mm')}else{'never'}) }", timeout=30)
    rows = []
    for line in out.splitlines():
        f = line.split("|")
        if len(f) >= 3:
            # 'posts' now means VERIFIED-by-reading-the-code, not name-matched.
            rows.append({"name": f[0], "state": f[1], "last": f[2],
                         "posts": f[0] in EXTERNAL_JOBS})
    return rows


def probe_fusion():
    """Fusion summary — reads the JSON built by fusion_build.py, never walks disk."""
    for fn in ("fusion_inventory.json", "fusion_summary.json"):
        try:
            with open(os.path.join(ROOT, fn), encoding="utf-8") as f:
                d = json.load(f)
            s = d.get("summary", d)
            return {"totalFiles": s.get("totalFiles"), "totalBytes": s.get("totalBytes"),
                    "archivedFiles": s.get("archivedFiles"), "liveFiles": s.get("liveFiles"),
                    "duplicateGroups": s.get("duplicateGroups"), "duplicateFiles": s.get("duplicateFiles"),
                    "generatedAt": s.get("generatedAt"), "byMonth": s.get("byMonth", {}),
                    "topProjects": dict(list((s.get("byProject") or {}).items())[:12])}
        except Exception:
            continue
    return None


def probe_seo():
    try:
        with open(os.path.join(ROOT, "seo_audit.json"), encoding="utf-8") as f:
            d = json.load(f)
        return {"count": d.get("count"), "avgScore": d.get("avgScore"),
                "generatedAt": d.get("generatedAt"),
                "worst": [{k: p.get(k) for k in ("page", "score", "title")} for p in (d.get("pages") or [])[:5]]}
    except Exception:
        return None


# ── brain DB (SQLite — history + verdicts) ──────────────────────────────
DB_PATH  = os.path.join(ROOT, "brain.db")
DB_LOCK  = threading.Lock()
_DB_CONN = None

def _db():
    global _DB_CONN
    if _DB_CONN is not None:
        return _DB_CONN
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS probe_history(
        id INTEGER PRIMARY KEY,
        at REAL NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phist ON probe_history(at,key)")
    conn.execute("""CREATE TABLE IF NOT EXISTS brain_verdicts(
        id INTEGER PRIMARY KEY,
        at REAL NOT NULL,
        verdicts_json TEXT NOT NULL
    )""")
    conn.commit()
    _DB_CONN = conn
    return conn

def log_probe(key, value):
    if value is None:
        return
    try:
        with DB_LOCK:
            db = _db()
            db.execute("INSERT INTO probe_history(at,key,value_json) VALUES(?,?,?)",
                       (time.time(), key, json.dumps(value, ensure_ascii=False)))
            db.execute("DELETE FROM probe_history WHERE at < ?", (time.time() - 7*86400,))
            db.commit()
    except Exception:
        pass

def _history(key, hours=24):
    cutoff = time.time() - hours * 3600
    try:
        with DB_LOCK:
            rows = _db().execute(
                "SELECT at, value_json FROM probe_history WHERE key=? AND at>? ORDER BY at",
                (key, cutoff)).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]
    except Exception:
        return []

BRAIN_PORT = 8092  # LFM2.5-2.6B — LIFE ISNAD brain (fast, Q4_K_M, 1.56 GB)

def _llm(messages, max_tokens=800, timeout=60):
    import urllib.request as _ur
    # LFM2.5-2.6B over-thinks: with a large context it fills a SMALL token budget
    # with pure chain-of-thought (60/60 reasoning, empty content, finish=length).
    # Measured 2026-10-03: the full probe_brain prompt needs max_tokens=800 to
    # reach the visible JSON answer. Keep temp 0.0 for deterministic JSON.
    body = json.dumps({
        "model": "local",
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }).encode()
    req = _ur.Request("http://127.0.0.1:%d/v1/chat/completions" % BRAIN_PORT,
                      data=body, method="POST",
                      headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            msg = d["choices"][0]["message"]
            return msg.get("content") or msg.get("reasoning_content") or ""
    except Exception:
        return None


# ── live project scanner ─────────────────────────────────────────────────
def probe_items():
    """Scan E:\\ABU top-level dirs → lightweight PROJECT_ITEMS-compatible rows."""
    repo_dirty = {}
    with LOCK:
        repos_probe = (STATE["probes"].get("repos") or {}).get("value") or []
    for r in repos_probe:
        repo_dirty[r["name"]] = r.get("dirty", 0)

    rows = []
    try:
        entries = sorted(os.scandir(ABU), key=lambda e: e.name)
    except OSError:
        return rows

    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith('.') or name.startswith('_ARCHIVE') or name == "life":
            continue
        try:
            children = list(os.scandir(entry.path))
            files    = sum(1 for c in children if c.is_file())
            dirs     = sum(1 for c in children if c.is_dir())
            mtimes   = []
            for c in children[:150]:
                try: mtimes.append(c.stat().st_mtime)
                except: pass
            last_mtime = max(mtimes) if mtimes else entry.stat().st_mtime
            last       = datetime.fromtimestamp(last_mtime).strftime("%Y-%m-%d")
            has_git    = os.path.isdir(os.path.join(entry.path, ".git"))
            dirty      = repo_dirty.get(name, 0)
            days_old   = (time.time() - last_mtime) / 86400

            if has_git and dirty > 20:
                status, pct = "wip", max(10, 100 - min(90, dirty))
            elif has_git:
                status, pct = "live", 100
            elif days_old < 7:
                status, pct = "live", 80
            elif days_old < 30:
                status, pct = "wip", 50
            else:
                status, pct = "parked", 20

            summary = "%d files, %d dirs. Last touched %s.%s%s" % (
                files, dirs, last,
                " Git repo." if has_git else "",
                " %d uncommitted." % dirty if dirty else "",
            )
            rows.append({
                "id": name.lower().replace(" ", "-"),
                "kind": "project",
                "title": name,
                "path": entry.path,
                "status": status,
                "pct": pct,
                "summary": summary,
                "revenue": "none",
                "emotion": "neutral",
                "truth": "LIVE",
                "touched": last,
                "steps": ["Review %d uncommitted files in %s" % (dirty, entry.path)] if dirty > 10 else [],
                "files": [],
                "blockers": ["%d uncommitted files" % dirty] if dirty > 50 else [],
            })
        except (PermissionError, OSError):
            continue

    return sorted(rows, key=lambda r: r.get("touched") or "", reverse=True)


# ── recursive learning brain ─────────────────────────────────────────────
def probe_brain():
    """Read 24h history, compare to current state, ask Qwen3-8B for verdicts.
    Each run trains the brain on what 'normal' looks like for this machine."""
    with LOCK:
        snap = {k: (STATE["probes"].get(k) or {}).get("value")
                for k in ("gpu", "ports", "procs", "host")}

    # Build baseline from GPU history
    gpu_hist = _history("gpu", hours=24)
    baselines = {}
    for g_idx in range(2):
        samples = []
        for _, vs in gpu_hist:
            if isinstance(vs, list) and len(vs) > g_idx:
                mb = vs[g_idx].get("usedMB")
                if mb is not None:
                    samples.append(mb)
        if samples:
            baselines["gpu%d_used_mb" % g_idx] = {
                "min": min(samples), "max": max(samples),
                "mean": int(sum(samples) / len(samples)),
                "samples": len(samples),
            }

    # Port stability from history
    port_hist = _history("ports", hours=24)
    port_stability = {}
    for _, ports_val in port_hist[-30:]:
        if isinstance(ports_val, list):
            for p in ports_val:
                pn = p.get("p")
                if pn:
                    ps = port_stability.setdefault(pn, {"open": 0, "total": 0})
                    ps["total"] += 1
                    if p.get("open"):
                        ps["open"] += 1
    stability_pct = {
        str(p): round(100 * v["open"] / v["total"]) if v["total"] else 0
        for p, v in port_stability.items() if v["total"] >= 3
    }

    context = {
        "now": now(),
        "gpu": snap.get("gpu") or [],
        "ports_open": [p for p in (snap.get("ports") or []) if p.get("open")],
        "ports_down": [p.get("p") for p in (snap.get("ports") or []) if not p.get("open")],
        "host": snap.get("host") or {},
        "gpu_baselines_24h": baselines,
        "port_uptime_pct_24h": stability_pct,
    }

    # Build ISNAD chain from history — what sources confirm what
    isnad_chain = []
    for key in ("gpu", "ports", "procs", "host", "items"):
        h = _history(key, hours=2)
        if len(h) >= 2:
            first_at = datetime.fromtimestamp(h[0][0]).strftime("%H:%M")
            last_at  = datetime.fromtimestamp(h[-1][0]).strftime("%H:%M")
            isnad_chain.append({"source": key, "samples": len(h),
                                 "span": "%s→%s" % (first_at, last_at),
                                 "verified": True})
        else:
            isnad_chain.append({"source": key, "samples": len(h), "verified": False})

    context["isnad_chain"] = isnad_chain

    prompt = """You are the ABUZ8 LIFE ISNAD verification brain (LFM2.5-2.6B).

ISNAD = chain of verified sources. Your job:
1. Verify each data source has a confirmed measurement chain (>=2 samples in last 2h)
2. Detect anomalies by comparing current state to 24h learned baselines
3. Flag unverified sources (isnad_chain.verified=false) as isnad-break kind
4. Report what the machine has LEARNED about itself from the history

Machine state + learned baselines + ISNAD chain:
%s

Analyze the machine state above and output a JSON object.

The JSON object has three keys:
- "isnad_status" is a string: "verified" if all sources have >=2 samples, "partial" if some do, "broken" if none do
- "learned" is a string: one sentence describing the 24h baseline pattern for this machine
- "verdicts" is an array of up to 6 objects, each with "kind" (ok/warn/critical/isnad-break), "subject" (real name), and "note" (real numbers)

Respond with only the JSON object.""" % json.dumps(context, indent=2)

    raw = _llm([
        {"role": "system", "content": "Output only JSON. No explanation, no markdown."},
        {"role": "user", "content": prompt}
    ], max_tokens=800, timeout=60)
    if not raw:
        return {"verdicts": [], "learned": None, "error": "brain :%d not reachable" % BRAIN_PORT}

    # Strip thinking tags and markdown fences
    text = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:]).rstrip("`").strip()

    # Parse: try full text, then brace-extract, then bracket-extract
    result = None
    for candidate in [text] + re.findall(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                result = parsed
                break
        except Exception:
            continue
    if result is None:
        brace_start = text.find('{')
        brace_end = text.rfind('}')
        if brace_start >= 0 and brace_end > brace_start:
            try:
                result = json.loads(text[brace_start:brace_end+1])
            except Exception:
                pass
    # If model returned a single verdict object, wrap it
    if result is not None and isinstance(result, dict) and "kind" in result and "verdicts" not in result:
        result = {"verdicts": [result], "isnad_status": "partial", "learned": None}
    # If model returned nothing useful
    if result is None:
        result = {"verdicts": [], "error": "parse failed", "raw": text[:300]}
    # Ensure proper envelope keys exist
    if "verdicts" not in result:
        result["verdicts"] = []
    if "isnad_status" not in result or not result.get("isnad_status"):
        kinds = [v.get("kind","ok") for v in result.get("verdicts",[]) if isinstance(v,dict)]
        breaks = sum(1 for k in kinds if k == "isnad-break")
        result["isnad_status"] = "broken" if breaks == len(kinds) and kinds else ("partial" if breaks else "verified")

    try:
        with DB_LOCK:
            db = _db()
            db.execute("INSERT INTO brain_verdicts(at,verdicts_json) VALUES(?,?)",
                       (time.time(), json.dumps(result, ensure_ascii=False)))
            db.execute("DELETE FROM brain_verdicts WHERE at < ?", (time.time() - 86400,))
            db.commit()
    except Exception:
        pass

    return result


def file_hash_info(path):
    """On-demand sha256 + mtime diff + git info for ONE path. Read-only.
    Refuses anything outside E:\\ABU or C:\\Users\\wirec owned roots."""
    try:
        ap = os.path.abspath(path)
    except Exception:
        return None
    ok_roots = (r"E:\ABU", r"C:\Users\wirec\Documents", r"C:\Users\wirec\Desktop")
    if not any(ap.lower().startswith(r.lower() + os.sep) or ap.lower() == r.lower() for r in ok_roots):
        return {"error": "path outside allowlist"}
    try:
        st = os.stat(ap)
    except OSError as e:
        return {"error": str(e)}
    sha = None
    if st.st_size < 100 * 1024 * 1024:
        try:
            import hashlib as _hl
            h = _hl.sha256()
            with open(ap, "rb") as f:
                for ch in iter(lambda: f.read(65536), b""):
                    h.update(ch)
            sha = h.hexdigest()
        except Exception as e:
            sha = "error: %s" % e
    repo = None
    d = os.path.dirname(ap)
    while len(d) > 3:
        if os.path.isdir(os.path.join(d, ".git")):
            g = ["git", "-C", d]
            repo = {"root": d, "sha": run(g + ["rev-parse", "--short", "HEAD"], timeout=5),
                    "branch": run(g + ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5),
                    "log": run(g + ["log", "-3", "--format=%h %cs %s", "--", ap], timeout=8)}
            break
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return {"path": ap, "sha256": sha, "size": st.st_size,
            "created": datetime.fromtimestamp(st.st_ctime).isoformat()[:19],
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat()[:19],
            "mtimeDiffSec": int(st.st_mtime - st.st_ctime), "repo": repo}


# ── mission feeds: Kanban board + CRM lanes (2026-10-03) ───────────────────
# The year of work lives in two queryable stores, not in chat history:
#   Kanban SQLite: C:\Users\wirec\AppData\Local\hermes\kanban\boards\abuz8-empire\kanban.db
#   CRM snapshot:  E:\ABU\MISSION_CONTROL\ABUZ8-BRAIN\02-PROJECTS\mission_board_chats_20261003.csv
# Both are read-only feeds for LIFE. Nothing here writes to either store.
KANBAN_DB = r"C:\Users\wirec\AppData\Local\hermes\kanban\boards\abuz8-empire\kanban.db"
MISSION_CSV = r"E:\ABU\MISSION_CONTROL\ABUZ8-BRAIN\02-PROJECTS\mission_board_chats_20261003.csv"
MISSION_INDEX = r"E:\ABU\MISSION_CONTROL\ABUZ8-BRAIN\02-PROJECTS\MISSION_INDEX_20261003.json"
WAR_PLAN = r"E:\ABU\MISSION_CONTROL\ABUZ8-BRAIN\02-PROJECTS\war_plan_20261003.json"
PROJECTS_JSON = r"E:\ABU\MISSION_CONTROL\ABUZ8-BRAIN\02-PROJECTS\projects_20261003.json"


def probe_missions():
    """Kanban board state + CRM snapshot counts. Read-only; empty on any error."""
    out = {"board": "abuz8-empire", "tasks": [], "counts": {}, "crm": {},
           "war": [], "projects": []}
    try:
        db = sqlite3.connect(KANBAN_DB, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id,title,status,assignee,priority,created_at,completed_at,result "
            "FROM tasks WHERE status != 'archived' ORDER BY priority DESC").fetchall()
        db.close()
        for r in rows:
            out["tasks"].append({
                "id": r["id"], "title": r["title"], "status": r["status"],
                "priority": r["priority"],
                "result": (r["result"] or "")[:200],
            })
        by_status = {}
        for t in out["tasks"]:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        out["counts"] = {"total": len(out["tasks"]), "by_status": by_status}
    except Exception as e:
        out["error"] = "kanban: %s" % str(e)[:120]
        return out
    try:
        with open(MISSION_INDEX, encoding="utf-8") as f:
            idx = json.load(f)
        out["crm"] = {"chats": idx.get("counts", {}).get("chats"),
                      "by_status": idx.get("counts", {}).get("by_status"),
                      "by_priority": idx.get("counts", {}).get("by_priority"),
                      "by_project": idx.get("counts", {}).get("by_project"),
                      "channels_month1_total": idx.get("channels_month1_total"),
                      "packets_on_disk": idx.get("packets_on_disk"),
                      "war_steps": idx.get("war_steps"),
                      "projects": idx.get("projects")}
    except Exception as e:
        out["crm"] = {"error": "index: %s" % str(e)[:120]}
    try:
        with open(WAR_PLAN, encoding="utf-8") as f:
            out["war"] = [{"step": w.get("step"), "hour": w.get("hour"),
                           "title": w.get("title"), "owner": w.get("owner")}
                          for w in json.load(f)]
    except Exception:
        pass
    try:
        with open(PROJECTS_JSON, encoding="utf-8") as f:
            out["projects"] = [{"slug": p.get("slug"), "name": p.get("name"),
                                "stats": p.get("stats")}
                               for p in json.load(f)]
    except Exception:
        pass
    return out


# ── tiered scheduler ────────────────────────────────────────────────────
# cheap probes run often; expensive ones rarely. Nothing here is worth
# spinning the CPU for — the whole point is a quiet background instrument.
TIERS = [
    ("gpu",      probe_gpu,      5),
    ("ports",    probe_ports,    10),
    ("procs",    probe_procs,    15),
    ("host",     probe_host,     15),
    ("volumes",  probe_volumes,  120),
    ("repos",    probe_repos,    180),
    ("secrets",  probe_secrets,  300),
    ("schedule", probe_schedule, 300),
    ("fusion",   probe_fusion,   300),
    ("seo",      probe_seo,      300),
    ("missions", probe_missions, 60),
    ("items",    probe_items,     60),
    ("brain",    probe_brain,    120),
]

STATE = {"started": now(), "probes": {}}
LOCK = threading.Lock()


def worker(key, fn, every):
    while True:
        t0 = time.time()
        try:
            val = fn()
        except Exception as e:
            val, err = None, str(e)
        else:
            err = None
        # A failed probe keeps the previous value; it only updates the error and age.
        with LOCK:
            prev = STATE["probes"].get(key, {})
            if val:
                prev["value"] = val
                log_probe(key, val)
            prev["at"] = now()
            prev["ms"] = int((time.time() - t0) * 1000)
            prev["err"] = err
            prev["every"] = every
            STATE["probes"][key] = prev
        time.sleep(every)


def snapshot():
    with LOCK:
        out = {"ok": True, "at": now(), "started": STATE["started"], "port": PORT, "probes": {}}
        t = time.time()
        for k, v in STATE["probes"].items():
            try:
                age = t - datetime.fromisoformat(v["at"]).timestamp()
            except Exception:
                age = None
            out["probes"][k] = {"value": v.get("value"), "at": v.get("at"),
                                "age_s": round(age, 1) if age is not None else None,
                                "ms": v.get("ms"), "every": v.get("every"), "err": v.get("err")}
        return out


# ── actions ─────────────────────────────────────────────────────────────
# A deliberately small, allowlisted set of things this daemon may do. The client
# sends an ID, never a command — no free text ever reaches a shell.
# Probes and the brain are read-only forever: NOTHING here acts on its own.
# The single hard rule: no delete without a human triple-yes, and even then
# the file is moved to quarantine, never unlinked — restore is moving it back.
#
# Gates escalate with blast radius:
#   tier1  local and reversible          → one confirmation (open a document)
#   tier2  runs code / persistent config → type CONFIRM (launch an app: type RUN)
#   tier3  speaks publicly as Ahmad      → type the exact task name, AND the
#          daemon must be started with --allow-external (off by default)
#   tier4  delete                        → triple-yes (CONFIRM → exact file name
#          → DELETE + token), then quarantine, never unlink
#
# CSRF: /api/act is same-origin only and needs a single-use nonce. Read endpoints
# stay open so the app still works from file://, but it can only ever read there.

ALLOW_EXTERNAL = "--allow-external" in sys.argv


def _is_elevated():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


ELEVATED = _is_elevated()
AUDIT = os.path.join(ROOT, "actions.log")
ORIGIN = "http://%s:%d" % (HOST, PORT)

# Jobs that actually send outward. This list is now derived from READING each target
# script, not from the task name — a name-based guess was wrong in BOTH directions:
#
#   Wrongly flagged (they send nothing):
#     AbuZait Morning/Midday/Evening Brief — the target was neutered 2026-06-16 and is
#       now a 27-line no-op that prints "sent nothing" and exits. All three share it.
#     ABUZ8_X_AGENT_OS_DAILY / _RELENTLESS — 379 lines, stdlib only, no X API and no
#       OAuth anywhere. They DRAFT markdown queues. The name lies.
#     ABUZ8_QEB_MorningBrief — pure local SQLite, loopback only.
#
#   Wrongly missed (these genuinely speak as Ahmad):
#     ABUZ8AI Judge   — hardcoded Telegram token, POSTs sendMessage every ~6h
#     Hermes_Gateway  — live Telegram bot, provably sent outward this morning
#     Watchdog        — LotVue app: live sk_live_ Stripe key + Gmail SMTP, 0.0.0.0:8360
#     AbuZ8-Autostart — writes an inbound firewall rule and rebinds ComfyUI to 0.0.0.0
#     AbuZ8-ChromeDebug — unauthenticated DevTools on :9222 over live logged-in sessions
EXTERNAL_JOBS = {
    "ABUZ8AI Judge",
    "Hermes_Gateway",
    "Watchdog",
    "AbuZ8-Autostart",
    "AbuZ8-ChromeDebug",
    "ABUZ8_Nemotron",   # binds --host 0.0.0.0 with no api-key: a 30B brain on the LAN
}

PWSH = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]

STATIC_ACTIONS = {
    "svc.comfyui.start": {
        "tier": 1, "title": "Start ComfyUI keep-alive",
        "why": "Brings :8188 back up. The render pipeline is down despite a keep-alive that claims permanent uptime.",
        "needs": r"E:\ABU\ops\comfyui-keepalive.ps1",
        # MUST be pwsh, not powershell. The script is valid UTF-8 and contains em-dashes;
        # Windows PowerShell 5.1 reads a no-BOM .ps1 as cp1252, which turns each em-dash
        # into a smart quote that opens a phantom string and fails the parse at line 49.
        # PS7 reads UTF-8 correctly. Pin the interpreter — this exact class of bug is why.
        "argv": PWSH + ["Start-Process pwsh -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','E:\\ABU\\ops\\comfyui-keepalive.ps1' -WindowStyle Hidden"],
        "undo": "Stop the pwsh keeper process, then the ComfyUI python it launched.",
    },
    "svc.ornith.start": {
        "tier": 1, "title": "Start Ornith brain (:8011)",
        "why": "Relaunches llama-server with the tuned Ornith config.",
        "needs": r"E:\ABU\ops\launch_ornith.cmd",
        "argv": PWSH + ["Start-Process cmd -ArgumentList '/c','E:\\ABU\\ops\\launch_ornith.cmd' -WindowStyle Minimized"],
        "undo": "Use 'Stop llama-server by name'.",
    },
    "svc.qadir.start": {
        "tier": 1, "title": "Start QADIR_CORE (:8900)",
        "why": "The living memory brain is dark. This starts its FastAPI process.",
        "needs": r"E:\ABU\QADIR_CORE\START_BRAIN_8900.bat",
        "argv": PWSH + ["Start-Process cmd -ArgumentList '/c','E:\\ABU\\QADIR_CORE\\START_BRAIN_8900.bat' -WindowStyle Minimized"],
        "undo": "Stop the python process it launches.",
    },
    "proc.llama.stop": {
        "tier": 1, "title": "Stop llama-server (by name)",
        "why": "Frees the ~27 GB it holds. Stopped BY PROCESS NAME, never by port — standing doctrine.",
        "argv": PWSH + ["Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force; 'stopped'"],
        "undo": "Start Ornith again.",
    },
    "git.gc.trunk": {
        "tier": 1, "title": "git gc — ABUZ8_OS_DIST (.git is 14.7 GB)",
        "why": "Repacks loose objects. This is the correct fix for .git bloat; it never deletes reachable history.",
        "argv": ["git", "-C", r"E:\ABU\ABUZ8_OS_DIST", "gc", "--auto"],
        "undo": "Not needed — packing is not destructive.",
        "slow": True,
    },
    "task.register.comfyui": {
        "tier": 2, "title": "Register ComfyUI keeper as a logon task",
        "why": "The keeper script's own header claims it is registered AtLogOn as "
               "ABUZ8-ComfyUI-KeepAlive. It never was, so ComfyUI does not survive a reboot. "
               "This registers it with pwsh pinned as the interpreter.",
        "needs": r"E:\ABU\ops\comfyui-keepalive.ps1",
        # Register-ScheduledTask writes to the machine task store and needs admin.
        # Surfaced up front so it is not discovered by a failure.
        "elevated": True,
        "argv": PWSH + [
            "$a = New-ScheduledTaskAction -Execute 'pwsh.exe' "
            "-Argument '-NoProfile -ExecutionPolicy Bypass -File \"E:\\ABU\\ops\\comfyui-keepalive.ps1\"';"
            "$t = New-ScheduledTaskTrigger -AtLogOn;"
            "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero);"
            "Register-ScheduledTask -TaskName 'ABUZ8-ComfyUI-KeepAlive' -Action $a -Trigger $t "
            "-Settings $s -Description 'Keeps ComfyUI alive on :8188' -Force | Out-Null; 'registered'"],
        "undo": "Unregister-ScheduledTask -TaskName 'ABUZ8-ComfyUI-KeepAlive'",
    },
    "git.gc.qadir": {
        "tier": 1, "title": "git gc — QADIR_CORE",
        "why": "Repacks loose objects (~257 MB of .git).",
        "argv": ["git", "-C", r"E:\ABU\QADIR_CORE", "gc", "--auto"],
        "undo": "Not needed — packing is not destructive.",
        "slow": True,
    },
}
# ── open + delete (Ahmad's doctrine: wide surface, gated by blast radius) ──
# Same managed roots the /api/hash reader uses. Anything outside is refused.
MANAGED_ROOTS = (r"E:\ABU", r"C:\Users\wirec\Documents", r"C:\Users\wirec\Desktop")
# Extensions that RUN code when opened: tier2, type RUN, still no arguments ever.
OPEN_EXEC_EXT = {".exe", ".bat", ".cmd", ".ps1", ".msi", ".vbs", ".vbe", ".js",
                 ".jse", ".wsf", ".wsh", ".com", ".scr", ".pif", ".reg", ".jar"}
# The daemon's own live files: deleting these by button is refused — do it by hand.
LIVE_FILES = {"lifed.py", "LIFE.html", "actions.log", "brain.db"}
QUARANTINE = os.path.join(ROOT, "quarantine")
DELETE_FLOWS = {}
DELETE_TTL = 180


def _resolve_managed(path):
    try:
        ap = os.path.abspath(path)
    except Exception:
        return None
    low = ap.lower()
    for r in MANAGED_ROOTS:
        rl = r.lower()
        if low == rl or low.startswith(rl + os.sep):
            return ap
    return None


def _fingerprint(ap):
    try:
        st = os.stat(ap)
        import hashlib as _hl
        h = _hl.sha256()
        h.update(ap.lower().encode())
        h.update(str(st.st_size).encode())
        h.update(str(int(st.st_mtime)).encode())
        return h.hexdigest()[:12], st.st_size
    except OSError:
        return None, 0


def known_tasks():
    """Live task names, so a scheduled-task action can only ever target a name the
    machine actually reports. This is the gate that keeps a task name from being
    an injection vector — it must match a probed name exactly."""
    v = (STATE["probes"].get("schedule") or {}).get("value") or []
    return {t["name"]: t for t in v}


def catalog():
    out = []
    for aid, a in STATIC_ACTIONS.items():
        need = a.get("needs")
        have_file = (not need) or os.path.exists(need)
        needs_admin = bool(a.get("elevated"))
        blocked = ""
        done = False
        # If the thing an action would create already exists, say so rather than
        # offering a button that does nothing useful.
        if aid == "task.register.comfyui" and "ABUZ8-ComfyUI-KeepAlive" in known_tasks():
            blocked = "already registered — verified in the task store"
            done = True
        elif not have_file:
            blocked = need
        elif needs_admin and not ELEVATED:
            blocked = ("needs admin. Either run E:\\ABU\\LIFE\\start-live-admin.cmd, "
                       "or better: the one-shot E:\\ABU\\LIFE\\register-keeper.ps1 elevated")
        out.append({"id": aid, "tier": a["tier"], "title": a["title"], "why": a["why"],
                    "undo": a.get("undo", ""), "slow": bool(a.get("slow")),
                    "elevated": needs_admin, "done": done,
                    "available": have_file and (not needs_admin or ELEVATED) and not done,
                    "needs": blocked})
    for name, t in sorted(known_tasks().items()):
        ext = name in EXTERNAL_JOBS
        running = t.get("state") == "Running"
        verb = "Disable" if running else "Enable"
        out.append({
            "id": ("task.disable:" if running else "task.enable:") + name,
            "tier": 3 if ext else 2,
            "title": "%s scheduled task — %s" % (verb, name),
            "why": ("This job POSTS or SENDS publicly as Ahmad. Enabling it makes the machine speak on his behalf."
                    if ext else "Windows Task Scheduler state change. Reversible."),
            "undo": "Run the opposite action.",
            "available": (not ext) or ALLOW_EXTERNAL,
            "needs": "" if (not ext) or ALLOW_EXTERNAL else "daemon must be started with --allow-external",
            "external": ext, "slow": False,
        })
    return out


def audit(entry):
    try:
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


NONCES = {}
NONCE_TTL = 120


def issue_nonce():
    import secrets
    n = secrets.token_urlsafe(18)
    NONCES[n] = time.time() + NONCE_TTL
    for k, exp in list(NONCES.items()):
        if exp < time.time():
            NONCES.pop(k, None)
    return n


def burn_nonce(n):
    exp = NONCES.pop(n, None)
    return bool(exp and exp >= time.time())


def do_action(body):
    aid = str(body.get("id", ""))
    confirm = str(body.get("confirm", ""))
    rec = {"at": now(), "id": aid, "ok": False, "detail": ""}

    if aid in STATIC_ACTIONS:
        a = STATIC_ACTIONS[aid]
        need = a.get("needs")
        if need and not os.path.exists(need):
            rec["detail"] = "required file missing: " + need
            audit(rec); return rec
        if confirm != "CONFIRM":
            rec["detail"] = "confirmation required"
            audit(rec); return rec
        ok, detail = run_checked(a["argv"], timeout=180 if a.get("slow") else 30)
        rec["ok"] = ok
        rec["detail"] = detail
        audit(rec); return rec

    if aid.startswith("task.enable:") or aid.startswith("task.disable:"):
        enable = aid.startswith("task.enable:")
        name = aid.split(":", 1)[1]
        tasks = known_tasks()
        if name not in tasks:
            # never pass an unverified string to the shell
            rec["detail"] = "unknown task name — refused"
            audit(rec); return rec
        ext = name in EXTERNAL_JOBS
        if ext and not ALLOW_EXTERNAL:
            rec["detail"] = ("this job posts publicly as Ahmad; daemon must be started "
                             "with --allow-external to arm it")
            audit(rec); return rec
        # tier3 requires the exact task name typed back; tier2 requires CONFIRM
        expect = name if ext else "CONFIRM"
        if confirm != expect:
            rec["detail"] = "confirmation mismatch — expected %s" % ("the exact task name" if ext else "CONFIRM")
            audit(rec); return rec
        verb = "Enable-ScheduledTask" if enable else "Disable-ScheduledTask"
        ok, detail = run_checked(PWSH + ["%s -TaskName %s -ErrorAction Stop | Out-Null; 'ok'"
                                         % (verb, json.dumps(name))], timeout=30)
        rec["ok"] = ok
        rec["detail"] = detail
        rec["external"] = ext
        audit(rec); return rec

    if aid.startswith("open.file:"):
        ap = _resolve_managed(aid[len("open.file:"):])
        if not ap or not os.path.isfile(ap):
            rec["detail"] = "outside managed roots or not a file"
            audit(rec); return rec
        ext = os.path.splitext(ap)[1].lower()
        if ext in OPEN_EXEC_EXT:
            if confirm != "RUN":
                rec["detail"] = "this launches code \u2014 type RUN to proceed (no arguments are ever passed)"
                audit(rec); return rec
        elif confirm != "CONFIRM":
            rec["detail"] = "confirmation required"
            audit(rec); return rec
        try:
            os.startfile(ap)  # OS-registered handler only. No shell, no arguments.
        except Exception as e:
            rec["detail"] = "open failed: %s" % e
            audit(rec); return rec
        rec["ok"] = True
        rec["detail"] = "opened with the registered handler"
        audit(rec); return rec

    if aid.startswith("delete.file:"):
        ap = _resolve_managed(aid[len("delete.file:"):])
        if not ap or not os.path.isfile(ap):
            rec["detail"] = "outside managed roots or not a file"
            audit(rec); return rec
        if os.path.basename(ap) in LIVE_FILES:
            rec["detail"] = "live instrument file \u2014 delete by hand, never by button"
            audit(rec); return rec
        fp, size = _fingerprint(ap)
        if not fp:
            rec["detail"] = "cannot fingerprint \u2014 refused"
            audit(rec); return rec
        flow = DELETE_FLOWS.get(fp)
        if flow and flow.get("expires", 0) < time.time():
            flow = None
        if flow and flow.get("path") != ap:
            flow = None  # fingerprint now points elsewhere \u2014 restart
        if confirm == "CONFIRM" and flow is None:
            DELETE_FLOWS[fp] = {"path": ap, "step": 2, "expires": time.time() + DELETE_TTL}
            rec["ok"] = True
            rec["detail"] = ("STEP2 fp=%s name=%s size=%d \u2014 yes #2: type the exact file name"
                             % (fp, os.path.basename(ap), size))
            audit(rec); return rec
        if flow and flow.get("step") == 2 and confirm == os.path.basename(ap):
            import secrets as _sc
            tok = _sc.token_urlsafe(9)
            flow["step"] = 3
            flow["token"] = tok
            flow["expires"] = time.time() + DELETE_TTL
            rec["ok"] = True
            rec["detail"] = ("STEP3 token=%s \u2014 yes #3: type DELETE %s to quarantine (reversible)"
                             % (tok, tok))
            audit(rec); return rec
        if flow and flow.get("step") == 3 and confirm == "DELETE " + str(flow.get("token")):
            import shutil as _sh
            try:
                os.makedirs(QUARANTINE, exist_ok=True)
                dest = os.path.join(QUARANTINE, "%s.%s.quarantined"
                                    % (datetime.now().strftime("%Y%m%d-%H%M%S"),
                                       os.path.basename(ap)))
                _sh.move(ap, dest)
            except Exception as e:
                rec["detail"] = "quarantine failed: %s" % e
                audit(rec); return rec
            DELETE_FLOWS.pop(fp, None)
            rec["ok"] = True
            rec["detail"] = "quarantined to %s \u2014 restore by moving it back" % dest
            audit(rec); return rec
        DELETE_FLOWS.pop(fp, None)
        rec["detail"] = "triple-yes mismatch \u2014 flow burned, start over with CONFIRM"
        audit(rec); return rec

    rec["detail"] = "not in the allowlist"
    audit(rec); return rec


# ── http ────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype, cors=True):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Read endpoints are open so the app still works from file://.
        # Action endpoints pass cors=False, so only the page this daemon itself
        # serves can act — a file:// copy or any other site can read but never do.
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _same_origin(self):
        """Refuse anything a *browser* originated from somewhere other than the page
        this daemon serves.

        Subtlety that cost a bug: browsers omit Origin entirely on same-origin GET,
        so requiring Origin == ORIGIN wrongly blocked the app's own nonce fetch while
        letting a curl that spoofed the header through. Sec-Fetch-Site is the header
        that is actually always present in a browser, so it carries the decision and
        Origin is only checked when it exists.

        A local non-browser client with no headers is allowed. That is not the threat
        model — anything already running on this box can act on it directly. The gate
        exists to stop a random web page from driving localhost."""
        o = self.headers.get("Origin")
        if o is not None and o != ORIGIN:
            return False                       # cross-origin browser request, incl. file:// ('null')
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs not in ("same-origin", "none"):
            return False                       # cross-site / same-site, per the browser itself
        return True

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/api/brain", "/api/brain/"):
            try:
                with DB_LOCK:
                    rows = _db().execute(
                        "SELECT at, verdicts_json FROM brain_verdicts ORDER BY at DESC LIMIT 10"
                    ).fetchall()
                history = [{"at": datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d %H:%M"), **json.loads(r[1])} for r in rows]
                return self._send(200, json.dumps({"ok": True, "history": history}, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "err": str(e)}), "application/json")
        if path in ("/api/state", "/api/state/"):
            return self._send(200, json.dumps(snapshot()), "application/json; charset=utf-8")
        if path in ("/api/fusion", "/api/fusion/"):
            try:
                with open(os.path.join(ROOT, "fusion_inventory.json"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "application/json; charset=utf-8")
            except OSError:
                return self._send(404, json.dumps({"ok": False, "err": "run fusion_build.py"}), "application/json")
        if path in ("/api/hash", "/api/hash/"):
            import urllib.parse as _up
            qs = _up.urlparse(self.path).query
            target = _up.parse_qs(qs).get("path", [""])[0]
            if not target:
                return self._send(400, json.dumps({"ok": False, "err": "need ?path="}), "application/json")
            return self._send(200, json.dumps({"ok": True, "file": file_hash_info(target)}, ensure_ascii=False), "application/json; charset=utf-8")
        if path in ("/api/health", "/api/health/"):
            return self._send(200, json.dumps({"ok": True, "at": now()}), "application/json")
        if path in ("/api/actions", "/api/actions/"):
            return self._send(200, json.dumps({"ok": True, "allowExternal": ALLOW_EXTERNAL,
                                               "openDelete": True,
                                               "actions": catalog()}),
                              "application/json; charset=utf-8")
        if path in ("/api/audit", "/api/audit/"):
            try:
                with open(AUDIT, encoding="utf-8") as f:
                    rows = [json.loads(x) for x in f.read().splitlines() if x.strip()]
            except OSError:
                rows = []
            return self._send(200, json.dumps({"ok": True, "entries": rows[-60:]}),
                              "application/json; charset=utf-8")
        if path in ("/api/nonce", "/api/nonce/"):
            if not self._same_origin():
                return self._send(403, json.dumps({"ok": False, "err": "same-origin only"}),
                                  "application/json", cors=False)
            return self._send(200, json.dumps({"ok": True, "nonce": issue_nonce()}),
                              "application/json", cors=False)
        if path in ("/", "/index.html", "/LIFE.html"):
            try:
                with open(APP, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(404, "LIFE.html not built yet — run build.ps1", "text/plain")
        if path in ("/living-brain.html",):
            try:
                with open(BRAIN, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(404, "living-brain.html not found", "text/plain")
        if path.startswith("/vendor/"):
            # Static assets only (the vendored Three.js build for the 3D Brain tab).
            # Resolve and re-check the path stays inside VENDOR — refuses any ../ escape.
            rel = path[len("/vendor/"):]
            fp = os.path.normpath(os.path.join(VENDOR, rel))
            if os.path.commonpath([fp, VENDOR]) != VENDOR:
                return self._send(403, "forbidden", "text/plain")
            ctype = ("application/javascript; charset=utf-8" if fp.endswith(".js")
                     else "application/octet-stream")
            try:
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), ctype)
            except OSError:
                return self._send(404, "not found", "text/plain")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/act", "/api/act/"):
            return self._send(404, "not found", "text/plain")
        if not self._same_origin():
            audit({"at": now(), "id": "?", "ok": False,
                   "detail": "refused: origin %r" % self.headers.get("Origin")})
            return self._send(403, json.dumps({"ok": False, "detail":
                              "actions are same-origin only — open the app from http://127.0.0.1:4310"}),
                              "application/json", cors=False)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 8192)) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"ok": False, "detail": "bad body"}),
                              "application/json", cors=False)
        if not burn_nonce(str(body.get("nonce", ""))):
            return self._send(403, json.dumps({"ok": False, "detail":
                              "missing or expired nonce — reload and try again"}),
                              "application/json", cors=False)
        res = do_action(body)
        return self._send(200, json.dumps(res), "application/json; charset=utf-8", cors=False)

    def log_message(self, *a):
        pass  # a monitor that spams its own console is not a monitor


def main():
    for key, fn, every in TIERS:
        threading.Thread(target=worker, args=(key, fn, every), daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("ABUZ8 LIFE daemon")
    print("  app     http://%s:%d" % (HOST, PORT))
    print("  api     http://%s:%d/api/state" % (HOST, PORT))
    print("  bound to localhost only")
    print("  actions allowlisted, same-origin only, single-use nonce, audited to actions.log")
    if ALLOW_EXTERNAL:
        print("  !! --allow-external IS ON: jobs that post publicly as Ahmad can be armed")
    else:
        print("  external-posting jobs are LOCKED (start with --allow-external to unlock)")
    print("  ctrl-c to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
