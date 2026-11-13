# agent-status

Local dashboard of Claude Code sessions and their open tasks.

```
python3 agent_status.py        # http://localhost:8765
```

- **Sessions:** read from `~/.claude/projects/*/<session-id>.jsonl` (title, folder, last activity, PRs, last prompt).
- **Tasks:** checkboxes from `~/claude/status/<session-id>.md`. View-only: Claude ticks them in the file.
- **Refresh:** every 5s while the tab is visible, 30s while hidden; the Refresh button forces a rescan.
- Index lives in `agent-status.db` (gitignored). Stdlib only, no installs.

## Start at login (macOS)

Create `~/Library/LaunchAgents/com.cuotos.agent-status.plist` that runs
`python3 /path/to/agent_status.py` with `RunAtLoad` and `KeepAlive` set, then:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cuotos.agent-status.plist
```

After changing the code, restart it with
`launchctl kickstart -k gui/$(id -u)/com.cuotos.agent-status`.
