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


def parse_status(path):
    """Return (title, tasks). tasks = [(line_no, section, text, done)]."""
    title, section, tasks = None, None, []
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        if title is None and line.startswith("# "):
            title = line[2:].strip()
            continue
        h = HEADING.match(line)
        if h:
            section = h.group(1).strip()
            continue
        m = CHECKBOX.match(line)
        if m:
            tasks.append((n, section, m.group(4).strip(), m.group(2).lower() == "x"))
    return title, tasks


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
    title, tasks = parse_status(path)
    conn.execute("INSERT OR IGNORE INTO sessions (id, prompts) VALUES (?, 0)", (sid,))
    conn.execute("UPDATE sessions SET status_path=?, status_mtime=?, status_title=? WHERE id=?",
                 (str(path), path.stat().st_mtime, title, sid))
    conn.execute("DELETE FROM tasks WHERE session_id=?", (sid,))
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?,?)",
                     [(sid, n, sec, text, int(done)) for n, sec, text, done in tasks])


def sessions_payload():
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
            "title": s["title"] or s["status_title"] or (s["first_prompt"] or "")[:80] or s["id"],
            "cwd": s["cwd"], "entrypoint": s["entrypoint"],
            "first_ts": s["first_ts"], "last_ts": s["last_ts"], "prompts": s["prompts"],
            "last_prompt": s["last_prompt"], "prs": json.loads(s["prs"] or "{}"),
            "status_path": s["status_path"],
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#faf6ec;--card:#fffdf7;--fg:#2b2b2b;--muted:#5f5d57;--line:#e6dfcf;--accent:#1f5fbf;--warn:#a84f0c;--chip:#f1ead9}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a17;--card:#24231f;--fg:#e8e4d9;--muted:#b3aea2;--line:#3a3832;--accent:#8fb6ff;--warn:#f0a35e;--chip:#2f2d28}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:18px/1.7 "Atkinson Hyperlegible",Verdana,sans-serif;letter-spacing:.01em;word-spacing:.08em}
header{position:sticky;top:0;background:var(--bg);border-bottom:2px solid var(--line);padding:14px 16px;display:flex;gap:16px;flex-wrap:wrap;align-items:center;z-index:1}
h1{font-size:22px;margin:0 8px 0 0}
input[type=search]{flex:1;min-width:180px;padding:8px 12px;font:inherit;border:2px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
label{user-select:none;cursor:pointer}
input[type=checkbox]{width:18px;height:18px;vertical-align:-3px}
button{font:inherit;border:2px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:6px 14px;cursor:pointer}
main{max-width:760px;margin:0 auto;padding:20px 16px}
.summary{margin-bottom:20px}
.card{background:var(--card);border:2px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:20px}
.top{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.title{font-weight:700;font-size:20px;flex:1;min-width:200px}
.badge{background:var(--warn);color:#fff;border-radius:12px;padding:2px 12px;font-size:16px;font-weight:700}
ul{list-style:none;margin:10px 0 0;padding:0}
li{display:flex;gap:12px;align-items:flex-start;padding:6px 0}
li.done span{color:var(--muted);text-decoration:line-through}
li i{font-style:normal;color:var(--warn);width:1.2em;flex:none;font-weight:700}
li.done i{color:var(--muted)}
.sec{font-weight:700;margin-top:12px}
details{margin-top:12px;color:var(--muted);font-size:16px}
summary{cursor:pointer;width:max-content}
details p{margin:6px 0}
details code{background:var(--chip);padding:1px 6px;border-radius:4px;cursor:pointer}
code.resume{display:inline-block;margin-top:4px;padding:6px 10px;word-break:break-all;user-select:all}
a{color:var(--accent)}
</style></head>
<body>
<header>
  <h1>Agent Status</h1>
  <input type="search" id="q" placeholder="Filter sessions…">
  <label><input type="checkbox" id="openOnly" checked> Open tasks only</label>
  <label><input type="checkbox" id="showDone"> Show done tasks</label>
  <button id="refresh">Refresh</button>
</header>
<main><div class="summary" id="summary"></div><div id="list"></div></main>
<script>
let data = [];
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
// claude --resume only finds a session from the folder it was started in.
const resumeCmd = s => (s.cwd ? `cd '${s.cwd}' && ` : "") + `claude --resume ${s.id}`;
function copyText(el) {
  navigator.clipboard.writeText(el.textContent).then(() => {
    const old = el.textContent;
    el.textContent = "Copied ✓";
    setTimeout(() => { el.textContent = old; }, 1200);
  });
}
async function load(force) {
  const r = await fetch("/api/sessions" + (force ? "?refresh=1" : ""));
  data = await r.json();
  render();
}
function render() {
  const q = $("q").value.toLowerCase();
  const openOnly = $("openOnly").checked, showDone = $("showDone").checked;
  const rows = data.filter(s =>
    (!openOnly || s.open > 0) &&
    (!q || [s.title, s.cwd, s.last_prompt, ...s.tasks.map(t => t.text)].join(" ").toLowerCase().includes(q)));
  const totalOpen = data.reduce((n, s) => n + s.open, 0);
  $("summary").textContent = `${rows.length} sessions shown · ${totalOpen} open tasks across ${data.filter(s => s.open).length} sessions`;
  $("list").innerHTML = rows.map(s => {
    let sec = null;
    const tasks = s.tasks.filter(t => showDone || !t.done).map(t => {
      const head = t.section !== sec ? `<div class="sec">${esc(sec = t.section)}</div>` : "";
      return `${head}<li class="${t.done ? "done" : ""}"><i>${t.done ? "✓" : "○"}</i><span>${esc(t.text)}</span></li>`;
    }).join("");
    const prs = Object.entries(s.prs).map(([u, n]) => `<a href="${esc(u)}" target="_blank">${esc(n)}</a>`).join(", ");
    return `<div class="card">
      <div class="top"><span class="title">${esc(s.title)}</span>
        ${s.open ? `<span class="badge">${s.open} open</span>` : ""}</div>
      ${tasks ? `<ul>${tasks}</ul>` : ""}
      <details data-id="${s.id}" ${openDetails.has(s.id) ? "open" : ""}><summary>Details · ${ago(s.last_ts)}</summary>
        <p>Folder: ${esc(s.cwd || "")}</p>
        <p>${s.prompts} prompts${prs ? ` · PRs: ${prs}` : ""}</p>
        ${s.last_prompt ? `<p>Last prompt: ${esc(s.last_prompt)}</p>` : ""}
        <p>Resume (click to copy):<br><code class="resume" onclick="copyText(this)">${esc(resumeCmd(s))}</code></p>
      </details></div>`;
  }).join("") || "<p>Nothing to show.</p>";
}
["q", "openOnly", "showDone"].forEach(id => $(id).addEventListener("input", render));
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
