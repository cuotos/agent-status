# agent-status

Local dashboard of Claude Code sessions and their open tasks.

```
python3 agent_status.py        # http://localhost:8765
```

- **Sessions:** read from `~/.claude/projects/*/<session-id>.jsonl` (title, folder, last activity, PRs, last prompt).
- **Tasks:** checkboxes from `~/claude/status/<session-id>.md`. View-only: Claude ticks them in the file.
- **Refresh:** every 5s while the tab is visible, 30s while hidden; the Refresh button forces a rescan.
- Index lives in `agent-status.db` (gitignored). Stdlib only, no installs.
