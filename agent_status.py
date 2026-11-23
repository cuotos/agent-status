#!/usr/bin/env python3
"""agent-status: a local dashboard of Claude Code sessions and their open tasks.

Reads:
  - ~/.claude/projects/*/<session-id>.jsonl   (session transcripts)
  - ~/claude/status/<session-id>.md            (status files with checkboxes)
Indexes them into SQLite and serves a single page on http://localhost:8765.
Stdlib only.
"""

import argparse
import json
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
STATUS_DIR = HOME / "claude" / "status"
DB_PATH = Path(__file__).with_name("agent-status.db")
RESCAN_SECONDS = 5

# Project dirs created by T3's title generator, not real sessions.
SKIP_PROJECT_MARKERS = ("t3code-claude-title",)

CHECKBOX = re.compile(r"^(\s*[-*]\s+\[)([ xX])(\]\s+)(.*)$")
HEADING = re.compile(r"^#{2,}\s+(.*)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    cwd TEXT,
    entrypoint TEXT,
    first_ts TEXT,
    last_ts TEXT,
    prompts INTEGER,
    first_prompt TEXT,
    last_prompt TEXT,
    prs TEXT,
    jsonl_path TEXT,
    jsonl_mtime REAL,
    status_path TEXT,
    status_mtime REAL,
    status_title TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    session_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    section TEXT,
    text TEXT,
    done INTEGER NOT NULL,
    PRIMARY KEY (session_id, line_no)
);
"""

_lock = threading.Lock()
_last_scan = 0.0


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def user_text(record):
    """Return the human-typed text of a user record, or None for tool results etc."""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if not parts:
            return None
        text = "\n".join(parts)
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<") or record.get("isMeta"):
        return None
    return text


def parse_transcript(path):
    info = {"title": None, "cwd": None, "entrypoint": None, "first_ts": None,
            "last_ts": None, "prompts": 0, "first_prompt": None,
            "last_prompt": None, "prs": {}}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # Cheap pre-filter: skip big records we never need.
            if line.startswith('{"type":"api-request'):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("type")
            ts = rec.get("timestamp")
            if ts and kind in ("user", "assistant"):
                info["first_ts"] = info["first_ts"] or ts
                info["last_ts"] = ts
            if kind == "custom-title" and rec.get("customTitle"):
                info["title"] = rec["customTitle"]
            elif kind == "last-prompt" and rec.get("lastPrompt"):
                info["last_prompt"] = rec["lastPrompt"]
            elif kind == "pr-link" and rec.get("prUrl"):
                info["prs"][rec["prUrl"]] = rec.get("prRepository", "") + "#" + str(rec.get("prNumber", ""))
            elif kind == "user":
                info["cwd"] = info["cwd"] or rec.get("cwd")
                info["entrypoint"] = info["entrypoint"] or rec.get("entrypoint")
                text = user_text(rec)
                if text:
                    info["prompts"] += 1
                    info["first_prompt"] = info["first_prompt"] or text[:300]
                    info["last_prompt"] = text[:300]
    return info


SUMMARY = re.compile(r"^\**(Next|About|Priority)\**:\**\s*(.*)$", re.I)


def parse_status(path):
    """Return (title, summary, tasks). summary = {"next": .., "about": ..};
    tasks = [(line_no, section, text, done)]."""
    title, section, tasks, summary = None, None, [], {}
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        if title is None and line.startswith("# "):
            title = line[2:].strip()
            continue
        s = SUMMARY.match(line.strip())
        if s and section is None:
            summary[s.group(1).lower()] = s.group(2).strip()
            continue
        h = HEADING.match(line)
        if h:
            section = h.group(1).strip()
            continue
        m = CHECKBOX.match(line)
        if m:
            tasks.append((n, section, m.group(4).strip(), m.group(2).lower() == "x"))
    return title, summary, tasks


def scan(force=False):
    global _last_scan
    with _lock:
        if not force and time.time() - _last_scan < RESCAN_SECONDS:
            return
        conn = db()
        known = {r["id"]: r for r in conn.execute("SELECT id, jsonl_mtime, status_mtime FROM sessions")}

        for path in PROJECTS_DIR.glob("*/*.jsonl"):
            if any(m in path.parent.name for m in SKIP_PROJECT_MARKERS):
                continue
            sid, mtime = path.stem, path.stat().st_mtime
            row = known.get(sid)
            if row and row["jsonl_mtime"] == mtime:
                continue
            info = parse_transcript(path)
            if not info["prompts"]:
                continue  # empty / aborted session
            conn.execute(
                """INSERT INTO sessions (id, title, cwd, entrypoint, first_ts, last_ts, prompts,
                       first_prompt, last_prompt, prs, jsonl_path, jsonl_mtime)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title, cwd=excluded.cwd,
                       entrypoint=excluded.entrypoint, first_ts=excluded.first_ts,
                       last_ts=excluded.last_ts, prompts=excluded.prompts,
                       first_prompt=excluded.first_prompt, last_prompt=excluded.last_prompt,
                       prs=excluded.prs, jsonl_path=excluded.jsonl_path,
                       jsonl_mtime=excluded.jsonl_mtime""",
                (sid, info["title"], info["cwd"], info["entrypoint"], info["first_ts"],
                 info["last_ts"], info["prompts"], info["first_prompt"], info["last_prompt"],
                 json.dumps(info["prs"]), str(path), mtime))

        if STATUS_DIR.is_dir():
            for path in STATUS_DIR.glob("*.md"):
                sid, mtime = path.stem, path.stat().st_mtime
                row = known.get(sid)
                if row and row["status_mtime"] == mtime:
                    continue
                load_status(conn, sid, path)
        conn.commit()
        conn.close()
        _last_scan = time.time()


def load_status(conn, sid, path):
    title, summary, tasks = parse_status(path)
    conn.execute("INSERT OR IGNORE INTO sessions (id, prompts) VALUES (?, 0)", (sid,))
    conn.execute("""UPDATE sessions SET status_path=?, status_mtime=?, status_title=?,
                        next_step=?, about=?, priority=? WHERE id=?""",
                 (str(path), path.stat().st_mtime, title,
                  summary.get("next"), summary.get("about"), summary.get("priority"), sid))
    conn.execute("DELETE FROM tasks WHERE session_id=?", (sid,))
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?,?)",
                     [(sid, n, sec, text, int(done)) for n, sec, text, done in tasks])


T3_DIR = HOME / ".t3" / "userdata"
T3_DB = T3_DIR / "statev2.sqlite"

# Top-level T3 threads (not subagents) that aren't archived or deleted, with the
# Claude session id behind each one. T3's internal schema: read-only, may change.
T3_QUERY = """
SELECT t.thread_id, t.title, t.updated_at,
       json_extract(t.payload_json, '$.snoozedUntil') AS snoozed_until,
       json_extract(t.payload_json, '$.settledAt') AS settled_at,
       json_extract(t.payload_json, '$.unsettledAt') AS unsettled_at,
       (SELECT json_extract(p.payload_json, '$.nativeThreadRef.nativeId')
          FROM orchestration_v2_projection_provider_threads p
         WHERE p.thread_id = t.thread_id
         ORDER BY p.updated_at DESC LIMIT 1) AS session_id
  FROM orchestration_v2_projection_threads t
 WHERE t.archived_at IS NULL AND t.deleted_at IS NULL
   AND json_extract(t.payload_json, '$.lineage.relationshipToParent') IS NULL
"""


def t3_threads():
    """Return (open_threads, snoozed_count), or None if T3's DB can't be read."""
    if not T3_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{T3_DB}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(T3_QUERY).fetchall()
        conn.close()
    except sqlite3.Error:
        return None
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    try:
        env = (T3_DIR / "environment-id").read_text().strip()
    except OSError:
        env = None
    threads, snoozed = [], 0
    for r in rows:
        settled = r["settled_at"] and (not r["unsettled_at"] or r["settled_at"] > r["unsettled_at"])
        if settled:
            continue
        if r["snoozed_until"] and r["snoozed_until"] > now:
            snoozed += 1
            continue
        threads.append({
            "thread_id": r["thread_id"], "title": r["title"], "updated_at": r["updated_at"],
            "session_id": r["session_id"],
            "link": f"t3-thread://v2/{env}/{r['thread_id']}" if env else None,
        })
    return threads, snoozed


def sessions_payload():
    """The board: open T3 threads if T3's DB is readable, else every session."""
    sessions = {s["id"]: s for s in all_sessions()}
    t3 = t3_threads()
    if t3 is None:
        return {"mode": "all", "sessions": list(sessions.values())}
    threads, snoozed = t3
    cards = []
    for th in threads:
        card = dict(sessions.get(th["session_id"]) or {
            "id": th["session_id"] or th["thread_id"], "cwd": None, "prompts": 0, "prs": {},
            "tasks": [], "open": 0, "last_ts": None, "first_prompt": None, "last_prompt": None,
            "next": None, "about": None, "priority": None, "status_path": None})
        card["title"] = th["title"]  # T3's current title, renames included
        card["t3_link"] = th["link"]
        card["last_ts"] = card["last_ts"] or th["updated_at"]
        cards.append(card)
    cards.sort(key=lambda c: c["last_ts"] or "", reverse=True)
    return {"mode": "t3", "snoozed": snoozed, "sessions": cards}


def all_sessions():
    conn = db()
    tasks = {}
    for t in conn.execute("SELECT * FROM tasks ORDER BY session_id, line_no"):
        tasks.setdefault(t["session_id"], []).append(
            {"line": t["line_no"], "section": t["section"], "text": t["text"], "done": bool(t["done"])})
    out = []
    for s in conn.execute("SELECT * FROM sessions ORDER BY COALESCE(last_ts, '') DESC"):
        t = tasks.get(s["id"], [])
        out.append({
            "id": s["id"],
            # Status file title first: T3 renames don't reach the transcript.
            "title": s["status_title"] or s["title"] or (s["first_prompt"] or "")[:80] or s["id"],
            "cwd": s["cwd"], "entrypoint": s["entrypoint"],
            "first_ts": s["first_ts"], "last_ts": s["last_ts"], "prompts": s["prompts"],
            "first_prompt": s["first_prompt"], "last_prompt": s["last_prompt"], "prs": json.loads(s["prs"] or "{}"),
            "status_path": s["status_path"],
            "next": s["next_step"], "about": s["about"], "priority": s["priority"],
            "tasks": t, "open": sum(1 for x in t if not x["done"]),
        })
    conn.close()
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif self.path.startswith("/api/sessions"):
            scan(force="refresh=1" in self.path)
            self.send(200, json.dumps(sessions_payload()))
        else:
            self.send(404, '{"error":"not found"}')


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Status</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%230969da'/%3E%3Ccircle cx='17' cy='20' r='5' fill='%23fff'/%3E%3Crect x='27' y='16' width='26' height='8' rx='4' fill='%23fff'/%3E%3Ccircle cx='17' cy='44' r='5' fill='none' stroke='%23fff' stroke-width='3'/%3E%3Crect x='27' y='40' width='18' height='8' rx='4' fill='%23fff' opacity='.6'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;600;700&display=swap" rel="stylesheet">
<style>
/* GitHub-style colours; follows the system light/dark setting. */
:root{--bg:#f6f8fa;--card:#ffffff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--accent:#0969da;--chip:#eff2f5;--nextbg:#ddf4ff}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#151b23;--fg:#f0f6fc;--muted:#9198a1;--line:#3d444d;--accent:#4493f8;--chip:#262c36;--nextbg:#121d2f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 "Fira Code",ui-monospace,SFMono-Regular,Menlo,monospace}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:16px;flex-wrap:wrap;align-items:center;z-index:1}
h1{font-size:17px;margin:0 8px 0 0}
input[type=search]{flex:1;min-width:180px;padding:6px 10px;font:inherit;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
label{user-select:none;cursor:pointer}
input[type=checkbox]{width:16px;height:16px;vertical-align:-3px;accent-color:var(--accent)}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:5px 12px;cursor:pointer}
main{max-width:1800px;margin:0 auto;padding:20px 16px}
.summary{margin-bottom:16px;color:var(--muted)}
#list{columns:400px;column-gap:16px}
.card{break-inside:avoid;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:16px;margin-bottom:16px;overflow-wrap:anywhere}
.top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.title{font-weight:700;font-size:15px;flex:1;min-width:200px;cursor:pointer}
.badge{background:var(--accent);color:#fff;border-radius:12px;padding:1px 10px;font-size:12px;font-weight:600}
.t3{font-size:12px;text-decoration:none}
.full{margin-top:8px;padding:10px 12px;background:var(--chip);border-radius:6px;white-space:pre-wrap;font-size:13px}
.next{margin-top:12px;padding:10px 12px;border-left:4px solid var(--accent);background:var(--nextbg);border-radius:6px;font-weight:600;line-height:1.5}
.next b{color:var(--accent)}
.next small{display:block;font-weight:400;font-size:12px;color:var(--muted)}
.none{margin-top:12px;color:var(--muted);font-style:italic}
.hint{margin-top:12px;color:var(--muted)}
.about{margin-top:10px}
.about b{color:var(--muted)}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font:inherit;font-size:.92em}
ul{list-style:none;margin:8px 0 0;padding:0}
li{display:flex;gap:10px;align-items:flex-start;padding:4px 0}
li.done span{color:var(--muted)}
li i{font-style:normal;color:var(--accent);width:1.2em;flex:none;font-weight:700}
li.done i{color:var(--muted)}
.sec{font-weight:700;margin-top:10px}
details{margin-top:10px;color:var(--muted);font-size:13px}
details ul{color:var(--fg)}
summary{cursor:pointer;width:max-content}
details p{margin:6px 0}
code.resume{display:inline-block;margin-top:4px;padding:6px 10px;word-break:break-all;user-select:all;cursor:pointer}
a{color:var(--accent)}
/* Table view: one row per thread, cells cut to one line, full text on hover. */
#list.table{columns:auto}
table{width:100%;border-collapse:collapse;table-layout:fixed;background:var(--card);border:1px solid var(--line);border-radius:6px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px}
td.title{font-weight:700;min-width:0}
.pri{display:inline-block;min-width:4.5em;text-align:center;border-radius:12px;padding:0 8px;font-size:12px;font-weight:700}
.pri.p3{background:#cf222e;color:#fff}
.pri.p2{background:#bf8700;color:#fff}
.pri.p1{background:var(--chip);color:var(--muted)}
.pri.set{text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:3px}
td.wait{background:var(--nextbg);font-weight:600;box-shadow:inset 4px 0 var(--accent)}
td.muted{color:var(--muted)}
td.copy{cursor:pointer;color:var(--accent)}
td.title{cursor:pointer}
/* Big hover popup for Thread and Waiting cells. */
#tip{position:fixed;z-index:5;max-width:560px;padding:14px 16px;background:var(--card);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.25);font-size:16px;line-height:1.6;overflow-wrap:anywhere;pointer-events:none}
#tip small{color:var(--muted);font-size:13px}
/* Full thread popup (click a thread). */
dialog#full{width:min(760px,92vw);max-height:85vh;padding:20px 24px;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;font-size:15px;overflow-wrap:anywhere}
dialog#full::backdrop{background:rgba(0,0,0,.45)}
dialog#full h2{font-size:18px;margin:0 32px 4px 0}
dialog#full .close{position:absolute;top:14px;right:16px}
.muted{color:var(--muted)}
button.small{margin-top:6px;padding:2px 10px;font-size:12px}
</style></head>
<body>
<header>
  <h1>Agent Status</h1>
  <input type="search" id="q" placeholder="Filter sessions…">
  <label id="openOnlyLabel"><input type="checkbox" id="openOnly" checked> Open tasks only</label>
  <button id="view">Table view</button>
  <button id="refresh">Refresh</button>
</header>
<main><div class="summary" id="summary"></div><div id="list"></div></main>
<div id="tip" hidden></div>
<dialog id="full" onclick="if (event.target === this) this.close()">
  <button class="close" onclick="$('full').close()">Close</button>
  <div id="fullBody"></div>
</dialog>
<script>
let data = [], mode = "all", snoozed = 0;
const openDetails = new Set();  // keep Details open across refreshes
document.addEventListener("toggle", e => {
  const id = e.target.dataset && e.target.dataset.id;
  if (id) e.target.open ? openDetails.add(id) : openDetails.delete(id);
}, true);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ago = ts => {
  if (!ts) return "";
  const m = (Date.now() - new Date(ts)) / 60000;
  if (m < 60) return Math.round(m) + "m ago";
  if (m < 1440) return Math.round(m / 60) + "h ago";
  return Math.round(m / 1440) + "d ago";
};
// Render **bold** and `code`; shorten long paths, full text on hover.
const shortPath = c => c.length > 40 && c.includes("/") ? "…/" + c.split("/").filter(Boolean).slice(-2).join("/") : c;
const md = text => esc(text)
  .replace(/`([^`]+)`/g, (_, c) => `<code title="${c}">${shortPath(c)}</code>`)
  .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
  // Bare URLs become a short "host ↗" link so they can't overflow the card.
  .replace(/(^|[\s(])(https?:\/\/[^\s)<]+)/g, (_, pre, u) =>
    `${pre}<a href="${u}" target="_blank" title="${u}">${u.split("/")[2]} ↗</a>`);
// The blue box means "waiting on Dan". "Next: none" (or nothing) means no box.
// Also catches lines written under the older rule ("Claude — …", "all done").
const NOT_WAITING = /^(none|nothing)\b|nothing waiting|all (items )?done|^claude\b/i;
const waiting = s => s.next && !NOT_WAITING.test(s.next.trim());
function nextBlock(s) {
  if (waiting(s)) return `<div class="next"><b>Waiting on you:</b> ${md(s.next)}</div>`;
  if (s.next) return `<div class="none">Nothing waiting on you</div>`;
  // No Next line yet (older session): show the first open task, plainly.
  const t = s.tasks.find(t => !t.done);
  return t ? `<div class="hint"><b>First open task:</b> ${md(t.text)}</div>` : "";
}
// Clicking a title shows the full first message.
const openFull = new Set();
const toggleFull = id => { openFull.has(id) ? openFull.delete(id) : openFull.add(id); render(); };
// claude --resume only finds a session from the folder it was started in.
const resumeCmd = s => (s.cwd ? `cd '${s.cwd}' && ` : "") + `claude --resume ${s.id}`;
function copyText(el) {
  navigator.clipboard.writeText(el.dataset.copy || el.textContent).then(() => {
    const old = el.textContent;
    el.textContent = "Copied ✓";
    setTimeout(() => { el.textContent = old; }, 1200);
  });
}
async function load(force) {
  const r = await fetch("/api/sessions" + (force ? "?refresh=1" : ""));
  const body = await r.json();
  data = body.sessions; mode = body.mode; snoozed = body.snoozed || 0;
  // With T3, the board is exactly the open threads, so the filter isn't needed.
  $("openOnlyLabel").hidden = mode === "t3";
  render();
}
// Cards or table; remembered in this browser.
let view = "cards";
try { view = localStorage.getItem("view") || "cards"; } catch (e) {}
const setViewLabel = () => { $("view").textContent = view === "table" ? "Card view" : "Table view"; };
setViewLabel();
$("view").onclick = () => {
  view = view === "table" ? "cards" : "table";
  try { localStorage.setItem("view", view); } catch (e) {}
  setViewLabel(); render();
};
// One line per cell. Thread and Waiting show a big popup on hover;
// smaller cells use the browser tooltip (title attribute).
const cell = (text, cls = "", html) =>
  `<td class="${cls}" title="${esc(text)}">${html ?? esc(text)}</td>`;
const tipCell = (tipHtml, cls, html, extra = "") =>
  `<td class="${cls}" data-tip="${esc(tipHtml)}" ${extra}>${html}</td>`;
document.addEventListener("mouseover", e => {
  const td = e.target.closest && e.target.closest("[data-tip]");
  const tip = $("tip");
  if (!td || !td.dataset.tip) { tip.hidden = true; return; }
  tip.innerHTML = td.dataset.tip;
  tip.hidden = false;
  const r = td.getBoundingClientRect();
  tip.style.left = Math.max(8, Math.min(r.left, innerWidth - tip.offsetWidth - 8)) + "px";
  const below = r.bottom + 6;
  tip.style.top = (below + tip.offsetHeight < innerHeight ? below : Math.max(8, r.top - tip.offsetHeight - 6)) + "px";
});
// Done tasks are hidden unless shown for that thread.
const showDoneFor = new Set();
// Open tasks first, then the Show/Hide done button, then done tasks below it.
function taskItems(list) {
  let sec = null;
  return list.map(t => {
    const head = t.section !== sec ? `<div class="sec">${esc(sec = t.section)}</div>` : "";
    return `${head}<li class="${t.done ? "done" : ""}"><i>${t.done ? "✓" : "○"}</i><span>${md(t.text)}</span></li>`;
  }).join("");
}
function taskList(s) {
  const open = s.tasks.filter(t => !t.done), done = s.tasks.filter(t => t.done);
  const show = showDoneFor.has(s.id);
  const btn = done.length ? `<button class="small" onclick="toggleDone('${s.id}')">${show ? "Hide" : "Show"} ${done.length} done</button>` : "";
  const doneList = show && done.length ? `<ul>${taskItems(done)}</ul>` : "";
  return open.length || btn ? `${open.length ? `<ul>${taskItems(open)}</ul>` : ""}${btn}${doneList}` : "";
}
function toggleDone(id) {
  showDoneFor.has(id) ? showDoneFor.delete(id) : showDoneFor.add(id);
  if ($("full").open) openThread(id);
  render();
}
// Clicking a thread opens everything about it, About included.
let byId = {};
function openThread(id) {
  const s = byId[id];
  if (!s) return;
  const tasks = taskList(s);
  $("tip").hidden = true;
  $("fullBody").innerHTML = `<h2>${esc(s.title)}</h2>
    ${waiting(s) ? `<div class="next"><b>Waiting on you:</b> ${md(s.next)}</div>` : `<div class="none">Nothing waiting on you</div>`}
    ${s.about ? `<div class="about"><b>About:</b> ${md(s.about)}</div>` : ""}
    ${tasks ? `<div class="sec">Tasks (${s.open} open)</div>${tasks}` : ""}
    ${s.first_prompt ? `<div class="sec">Your first message</div><div class="full">${esc(s.first_prompt)}</div>` : ""}
    <p class="muted">Folder: ${esc(s.cwd || "")} · active ${ago(s.last_ts)}</p>
    <p>Resume (click to copy):<br><code class="resume" onclick="copyText(this)">${esc(resumeCmd(s))}</code></p>`;
  if (!$("full").open) $("full").showModal();
}
// Rough priority, worked out here (nothing records one):
// High = waiting on you and idle 4h+, Medium = waiting on you, Low = nothing waiting.
const PRI_NAMES = {3: "High", 2: "Medium", 1: "Low"};
// A "**Priority:** high|medium|low" line in the status file (set when Dan
// tells Claude) wins over the guess.
const PRI_SET = {high: 3, medium: 2, med: 2, low: 1};
const setPriority = s => PRI_SET[(s.priority || "").trim().toLowerCase().split(/\W/)[0]];
function priority(s) {
  if (setPriority(s)) return setPriority(s);
  if (!waiting(s)) return 1;
  const hours = s.last_ts ? (Date.now() - new Date(s.last_ts)) / 36e5 : 0;
  return hours >= 4 ? 3 : 2;
}
const priBadge = s => setPriority(s)
  ? `<span class="pri p${priority(s)} set" title="Set by you">${PRI_NAMES[priority(s)]}</span>`
  : `<span class="pri p${priority(s)}">${PRI_NAMES[priority(s)]}</span>`;
function renderTable(rows) {
  byId = Object.fromEntries(rows.map(s => [s.id, s]));
  $("tip").hidden = true;
  const body = rows.map(s => {
    const open = s.tasks.filter(t => !t.done).map(t => "○ " + t.text).join("\n");
    const next = waiting(s) ? s.next : "";
    return `<tr>
      <td title="High: waiting on you, idle 4h+ · Medium: waiting on you · Low: nothing waiting">${priBadge(s)}</td>
      ${tipCell(`<b>${esc(s.title)}</b><br><small>Click to open</small>`, "title", esc(s.title), `onclick="openThread('${s.id}')"`)}
      ${next ? tipCell(md(next), "wait", md(next)) : "<td></td>"}
      ${cell(open, "muted", s.open ? `${s.open} open` : "")}
      ${cell(s.last_ts ? new Date(s.last_ts).toLocaleString() : "", "muted", ago(s.last_ts))}
      <td class="copy" title="${esc(resumeCmd(s))}" data-copy="${esc(resumeCmd(s))}" onclick="copyText(this)">copy</td>
    </tr>`;
  }).join("");
  $("list").innerHTML = rows.length ? `<table>
    <colgroup><col style="width:8%"><col style="width:30%"><col><col style="width:9%"><col style="width:8%"><col style="width:6%"></colgroup>
    <thead><tr><th>Priority</th><th>Thread</th><th>Waiting on you</th><th>Tasks</th><th>Active</th><th>Resume</th></tr></thead>
    <tbody>${body}</tbody></table>` : "<p>Nothing to show.</p>";
}
function render() {
  const q = $("q").value.toLowerCase();
  const openOnly = mode !== "t3" && $("openOnly").checked;
  const rows = data.filter(s =>
    (!openOnly || s.open > 0) &&
    (!q || [s.title, s.cwd, s.last_prompt, ...s.tasks.map(t => t.text)].join(" ").toLowerCase().includes(q)));
  rows.sort((a, b) => priority(b) - priority(a));  // highest first; otherwise keep server order (newest first)
  const totalOpen = data.reduce((n, s) => n + s.open, 0);
  $("summary").textContent = mode === "t3"
    ? `${rows.length} open T3 threads · ${totalOpen} open tasks${snoozed ? ` · ${snoozed} snoozed (hidden)` : ""}`
    : `${rows.length} sessions shown · ${totalOpen} open tasks across ${data.filter(s => s.open).length} sessions`;
  $("list").className = view;
  if (view === "table") return renderTable(rows);
  $("list").innerHTML = rows.map(s => {
    const tasks = taskList(s);
    const prs = Object.entries(s.prs).map(([u, n]) => `<a href="${esc(u)}" target="_blank">${esc(n)}</a>`).join(", ");
    return `<div class="card">
      <div class="top"><span class="title" title="Click for your first message" onclick="toggleFull('${s.id}')">${esc(s.title)}</span>
        <span title="High: waiting on you, idle 4h+ · Medium: waiting on you · Low: nothing waiting">${priBadge(s)}</span>
        ${s.open ? `<span class="badge">${s.open} open</span>` : ""}
</div>
      ${openFull.has(s.id) && s.first_prompt ? `<div class="full">${esc(s.first_prompt)}</div>` : ""}
      ${nextBlock(s) || (s.about ? "" : `<div class="none">No status yet</div>`)}
      ${s.about ? `<div class="about"><b>About:</b> ${md(s.about)}</div>` : ""}
      ${tasks ? `<details data-id="t-${s.id}" ${openDetails.has("t-" + s.id) ? "open" : ""}>
        <summary>Tasks (${s.open} open)</summary>${tasks}</details>` : ""}
      <details data-id="${s.id}" ${openDetails.has(s.id) ? "open" : ""}><summary>Details · ${ago(s.last_ts)}</summary>
        <p>Folder: ${esc(s.cwd || "")}</p>
        <p>${s.prompts} prompts${prs ? ` · PRs: ${prs}` : ""}</p>
        ${s.last_prompt ? `<p>Last prompt: ${esc(s.last_prompt)}</p>` : ""}
        <p>Resume (click to copy):<br><code class="resume" onclick="copyText(this)">${esc(resumeCmd(s))}</code></p>
      </details></div>`;
  }).join("") || "<p>Nothing to show.</p>";
}
["q", "openOnly"].forEach(id => $(id).addEventListener("input", render));
$("refresh").onclick = () => load(true);

// Poll every 5s while the tab is visible, every 30s while hidden.
let timer;
function schedule() {
  clearTimeout(timer);
  timer = setTimeout(async () => { await load().catch(() => {}); schedule(); },
                     document.hidden ? 30000 : 5000);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) load().catch(() => {});
  schedule();
});
load(true).finally(schedule);
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scan-only", action="store_true", help="index once and print a summary")
    args = ap.parse_args()

    conn = db()
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    for col in ("next_step", "about", "priority"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
            conn.execute("UPDATE sessions SET status_mtime=NULL")  # force re-read
    conn.commit()
    conn.close()
    scan(force=True)

    if args.scan_only:
        rows = sessions_payload()
        print(f"{len(rows)} sessions, {sum(r['open'] for r in rows)} open tasks")
        return
    print(f"agent-status on http://localhost:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
