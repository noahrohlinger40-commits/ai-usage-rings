# AI Usage Rings

A small always-on-top pill at the top of the screen with three rings: Claude Code, Fable (Claude's separate
weekly allowance on Max) and Codex. Each ring fills with how much of its usage window is burned, spins while the
agent works, and turns amber when Claude is waiting on you (a permission prompt). The Claude ring follows the
5-hour session window, the one that resets soonest; hover to see every window's percent and reset time.
Drag to move, double-click to refresh, right-click for Refresh and Quit. It draws sharp on scaled (high-DPI)
screens.

Personal rebuild of the idea behind [Codenotch](https://github.com/vinzdg/codenotch), one Python file,
standard library only.

## Run

    py -3 notch.py

Start it with Windows: put a shortcut to `notch.cmd` in the folder `shell:startup` opens.

## Where the numbers come from

- **Claude Code**: the login token in `~/.claude/.credentials.json`, sent to Anthropic's OAuth usage endpoint.
  The token is read only. It goes stale about eight hours after the last `claude` command; the pill then says
  "token expired" until you run any `claude` command again.
- **Codex**: the token in `~/.codex/auth.json`, sent to ChatGPT's usage endpoint. If that fails, the last
  rate-limit line in Codex's newest session log is shown instead, marked "from last session log".

Nothing is written back to either app, and tokens are never logged.

Both usage endpoints are the ones the apps themselves call, not documented public APIs, so they can change
without notice. If a ring stops updating, hover the pill: the error shows there.

## Working / waiting

- **Claude**: hooks. `py -3 notch.py --install-hooks` adds entries to `~/.claude/settings.json` (merged, never
  overwriting your own hooks; `--uninstall-hooks` removes exactly those). Prompt submit and tool use mean working,
  a permission or elicitation notification means waiting, Stop or an idle notification means idle.
  Takes effect in new Claude Code sessions.
- **Codex**: no hooks exist, so the tail of its newest session log is classified the way Codenotch does it.
  A tool call in flight and a tool waiting for your approval look the same, so Codex never shows amber.

## Check

    py -3 notch.py --selftest
