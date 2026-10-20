"""AI Usage Rings (Notch): a tiny always-on-top pill showing how much of your Claude Code, Fable and Codex usage is burned,
and whether each one is working, waiting on you, or idle.

    py -3 notch.py                  run the widget
    py -3 notch.py --install-hooks  add the state hooks to ~/.claude/settings.json (idempotent)
    py -3 notch.py --uninstall-hooks
    py -3 notch.py --hook EVENT     (called by Claude Code's hooks; stdin is the hook JSON)
    py -3 notch.py --selftest

Numbers come from the apps already on this PC: the token Claude Code keeps in ~/.claude/.credentials.json
and the one Codex keeps in ~/.codex/auth.json. Both are read only, never written or refreshed, and never logged.
Claude's state comes from Claude Code hooks; Codex has no hooks, so its state is read off the tail of its
newest session log (a heuristic: a tool call in flight looks the same as waiting for your approval).
"""
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", HOME)) / "notch"
CLAUDE_STATE = STATE_DIR / "claude-state.json"
CONFIG = STATE_DIR / "config.json"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
USAGE_EVERY, STATE_EVERY = 60, 2  # seconds
MARK = f'"{Path(__file__).resolve()}" --hook'  # how our hook entries are recognised in settings.json

# ---------- usage: Claude ----------

def claude_token():
    d = json.loads((HOME / ".claude" / ".credentials.json").read_text("utf-8"))["claudeAiOauth"]
    if not d.get("accessToken"):
        raise LookupError("signed out")
    if d.get("expiresAt", 0) / 1000 < time.time():
        raise LookupError("token expired: run any `claude` command")
    return d["accessToken"]


def _epoch(v):
    """resets_at as epoch seconds; accepts RFC 3339 text or a number (seconds or ms)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


CLAUDE_LABELS = {"five_hour": "Session (5h)", "session": "Session (5h)", "seven_day": "Weekly", "weekly_all": "Weekly",
                 "seven_day_opus": "Weekly (Opus)", "weekly_opus": "Weekly (Opus)", "weekly_scoped": "Weekly (model-scoped)"}
FABLE = "Fable weekly"  # the model-scoped weekly limit on Max (Fable gets up to 50% of the week); drawn as its own ring


def claude_ring(windows: list) -> list:
    """The Claude ring tracks the 5-hour session (the window that resets soonest), not whichever window is fullest.
    Weekly windows stay in the tooltip; they fill the ring only if the session window is missing."""
    return [w for w in windows if w[0] == CLAUDE_LABELS["session"]] or [w for w in windows if w[0] != FABLE]


def parse_claude(reply: dict) -> list:
    """[(label, percent, resets_at_epoch)] from {limits:[{kind,percent,resets_at}], five_hour:{utilization,resets_at}, ...}.
    `limits` is the main shape; five_hour/seven_day fill in only windows it did not already name."""
    out = []
    for lim in reply.get("limits") or []:
        kind = lim.get("kind", "")
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        label = f"{model} weekly" if kind == "weekly_scoped" and model else CLAUDE_LABELS.get(kind, kind.replace("_", " ").capitalize())
        out.append((label, float(lim.get("percent") or 0), _epoch(lim.get("resets_at"))))
    for kind in ("five_hour", "seven_day", "seven_day_opus"):
        w = reply.get(kind)
        if isinstance(w, dict) and CLAUDE_LABELS[kind] not in {label for label, _, _ in out}:
            out.append((CLAUDE_LABELS[kind], float(w.get("utilization") or 0), _epoch(w.get("resets_at"))))
    return out


def fetch_claude() -> list:
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage",
                                 headers={"Authorization": f"Bearer {claude_token()}", "anthropic-beta": "oauth-2025-04-20"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return parse_claude(json.loads(r.read()))


# ---------- usage: Codex ----------

def codex_token():
    auth = HOME / ".codex" / "auth.json"
    if not auth.exists():
        raise LookupError("codex signed out: run codex login")  # file is deleted on logout (and when a login is started)
    t = json.loads(auth.read_text("utf-8"))["tokens"]
    if not (t.get("access_token") and t.get("account_id")):
        raise LookupError("signed out")
    return t["access_token"], t["account_id"]


def codex_label(seconds) -> str:
    return {18000: "Session (5h)", 604800: "Weekly"}.get(seconds, f"{round((seconds or 0) / 3600)} h window")


def parse_codex(reply: dict) -> list:
    out = []
    for key in ("primary_window", "secondary_window"):
        w = (reply.get("rate_limit") or {}).get(key)
        if w:
            secs = w.get("limit_window_seconds")
            reset = w.get("reset_at") or (time.time() + w["reset_after_seconds"] if w.get("reset_after_seconds") else None)
            out.append((codex_label(secs), float(w.get("used_percent") or 0), _epoch(reset)))
    return out


def fetch_codex() -> list:
    token, account = codex_token()
    req = urllib.request.Request("https://chatgpt.com/backend-api/wham/usage",
                                 headers={"Authorization": f"Bearer {token}", "ChatGPT-Account-Id": account,
                                          "Accept": "application/json", "Cache-Control": "no-cache, no-store"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return parse_codex(json.loads(r.read()))


def codex_log_usage(text: str) -> list:
    """Fallback from the session log: the last token_count line carries rate_limits.primary/secondary."""
    for line in reversed(text.splitlines()):
        if '"token_count"' not in line:
            continue
        try:
            rl = json.loads(line)["payload"]["rate_limits"]
        except (ValueError, KeyError, TypeError):
            continue
        return [(codex_label((w.get("window_minutes") or 0) * 60), float(w.get("used_percent") or 0), _epoch(w.get("resets_at")))
                for w in (rl.get("primary"), rl.get("secondary")) if w]
    return []


# ---------- state: Claude (hooks) ----------

WAITING, WORKING, IDLE = "waiting", "working", "idle"
HOOK_STATE = {"UserPromptSubmit": WORKING, "PreToolUse": WORKING, "PostToolUse": WORKING, "Stop": IDLE,
              "Notification": WAITING, "IdleNotification": IDLE, "SessionEnd": None}


def _write_json(path: Path, data):
    """Atomic write. Another Notch or a reader may hold the target (os.replace then raises PermissionError on
    Windows): retry once, then give up quietly; the widget keeps drawing from memory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")  # per-process name so two writers never share a temp file
    for attempt in (0, 1):
        try:
            tmp.write_text(json.dumps(data), "utf-8")
            os.replace(tmp, path)
            return
        except OSError:
            if attempt:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            else:
                time.sleep(0.3)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def record_hook(event: str, payload: dict, now=None):
    """Called by the Claude Code hook: fold this session's new state into claude-state.json."""
    now = now or time.time()
    sessions = {k: v for k, v in _read_json(CLAUDE_STATE, {}).items() if now - v.get("at", 0) < 7200}
    sid = payload.get("session_id", "?")
    state = HOOK_STATE.get(event, WORKING)
    if state is None:
        sessions.pop(sid, None)
    else:
        sessions[sid] = {"state": state, "at": now, "cwd": payload.get("cwd", "")}
    _write_json(CLAUDE_STATE, sessions)
    return sessions


def claude_state(sessions: dict, now=None) -> str:
    """Any session waiting on you wins; else any session working in the last 10 min; else idle."""
    now = now or time.time()
    live = [v for v in sessions.values() if now - v.get("at", 0) < 600]
    if any(v["state"] == WAITING for v in live):
        return WAITING
    if any(v["state"] == WORKING for v in live):
        return WORKING
    return IDLE


HOOK_WIRING = (  # (Claude Code event, matcher or None, our event name)
    ("UserPromptSubmit", None, "UserPromptSubmit"), ("PreToolUse", "*", "PreToolUse"), ("PostToolUse", "*", "PostToolUse"),
    ("Notification", "permission_prompt|elicitation_dialog", "Notification"), ("Notification", "idle_prompt", "IdleNotification"),
    ("Stop", None, "Stop"), ("SessionEnd", None, "SessionEnd"))


def is_ours(entry: dict) -> bool:
    return any("notch.py" in h.get("command", "") and "--hook" in h.get("command", "") for h in entry.get("hooks", []))


def merge_hooks(settings: dict, python: str) -> dict:
    """Add our hook entries to a settings dict without touching anyone else's; idempotent."""
    hooks = remove_hooks(settings).setdefault("hooks", {})
    for event, matcher, ours in HOOK_WIRING:
        entry = {"hooks": [{"type": "command", "command": f'"{python}" {MARK} {ours}', "timeout": 5}]}
        if matcher:
            entry["matcher"] = matcher
        hooks.setdefault(event, []).append(entry)
    return settings


def remove_hooks(settings: dict) -> dict:
    for event, entries in list(settings.get("hooks", {}).items()):
        kept = [e for e in entries if not is_ours(e)]
        if kept:
            settings["hooks"][event] = kept
        else:
            del settings["hooks"][event]
    return settings


# ---------- state: Codex (session log tail) ----------

TOOL, THINK, SAY, END = "tool", "think", "say", "end"
BUSY_FOR = {TOOL: 600, THINK: 120, SAY: 4, END: 0}  # seconds of silence each step stays "working", per Codenotch


def codex_step(line: str):
    try:
        v = json.loads(line)
    except ValueError:
        return None
    p = v.get("payload") or {}
    kind, pt = v.get("type"), p.get("type", "")
    if kind == "turn_context":
        step = THINK
    elif kind == "response_item":
        step = {"function_call": TOOL, "local_shell_call": TOOL, "custom_tool_call": TOOL, "web_search_call": TOOL,
                "function_call_output": THINK, "custom_tool_call_output": THINK, "reasoning": THINK,
                "message": {"assistant": SAY, "user": THINK}.get(p.get("role"))}.get(pt)
    elif kind == "event_msg":
        step = {"turn_aborted": END, "task_complete": END, "task_started": THINK, "item_started": THINK, "exec_command_begin": THINK,
                "user_message": THINK, "agent_message": SAY, "agent_reasoning": THINK, "agent_reasoning_raw_content": THINK}.get(pt)
    else:
        step = None
    if step is None:
        return None
    ts = v.get("timestamp")
    return step, (_epoch(ts) if ts else 0)


def codex_state_from_tail(text: str, now=None) -> str:
    now = now or time.time()
    for line in reversed(text.splitlines()):
        if not line.strip():
            continue
        hit = codex_step(line)
        if hit:
            step, ts = hit
            return WORKING if now - ts < BUSY_FOR[step] else IDLE
    return IDLE


def newest_codex_log():
    root = HOME / ".codex" / "sessions"
    days = sorted((d for d in root.glob("*/*/*") if d.is_dir()), key=lambda d: d.parts[-3:])[-3:] if root.exists() else []
    files = [f for d in days for f in d.glob("rollout-*.jsonl")]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def read_tail(path: Path, size=65536) -> str:
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - size))
        return f.read().decode("utf-8", "replace")


# ---------- text helpers ----------

def reset_text(epoch, now=None) -> str:
    if not epoch:
        return ""
    now = now or time.time()
    left = epoch - now
    if left <= 0:
        return "resets now"
    if left < 86400:
        h, m = divmod(round(left / 60), 60)
        return f"resets in {h} h {m} min" if h else f"resets in {m} min"
    return "resets " + datetime.fromtimestamp(epoch).strftime("%a %I:%M %p").replace(" 0", " ")


BG, EDGE, TRACK, TEXT, MUTED = "#f3f4f6", "#d6d9df", "#dde0e6", "#16171b", "#6a6f7a"  # the pill's palette
FONTS = {"name": ("Segoe UI", 7), "pct": ("Segoe UI Semibold", 11)}


def ring_colour(percent: float, state: str) -> str:
    if state == WAITING:
        return "#d69a00"  # amber: it needs you
    return "#d9363e" if percent >= 90 else "#e57f10" if percent >= 70 else "#1fa970"


def blend(a: str, b: str, t: float) -> str:
    """Colour a mixed toward b by t (0..1): used to fake anti-aliasing with a soft halo."""
    pa, pb = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


def pill(c, w, h, fill, edge):
    """A stadium (full-radius pill): two end caps, a body, and top and bottom hairlines."""
    c.create_oval(0, 0, h - 1, h - 1, fill=fill, outline=edge)
    c.create_oval(w - h, 0, w - 1, h - 1, fill=fill, outline=edge)
    c.create_rectangle(h // 2, 0, w - h // 2, h, fill=fill, outline="")
    c.create_line(h // 2, 0, w - h // 2, 0, fill=edge)
    c.create_line(h // 2, h - 1, w - h // 2, h - 1, fill=edge)


def ring(c, cx, cy, R, wd, pct, state, spin, colour, track, bg, halo=True):
    """Usage ring: track, a soft halo under the arc (hides Tk's jagged edge), the arc, round end caps."""
    box = (cx - R, cy - R, cx + R, cy + R)
    c.create_oval(*box, outline=track, width=wd)
    if state == WORKING:
        start, ext = -spin, 80
    elif pct:
        start, ext = 90, -3.6 * min(pct, 99.99)
    else:
        return
    if halo:
        c.create_arc(*box, start=start, extent=ext, style="arc", outline=blend(colour, bg, .6), width=wd + 1)
    c.create_arc(*box, start=start, extent=ext, style="arc", outline=colour, width=wd)
    for a in (start, start + ext):                  # round caps
        x, y = cx + R * math.cos(math.radians(a)), cy - R * math.sin(math.radians(a))
        c.create_oval(x - wd / 2, y - wd / 2, x + wd / 2, y + wd / 2, fill=colour, outline=colour)


# ---------- the widget ----------

class Notch:
    POLL = 60

    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.cfg = _read_json(CONFIG, {})
        self.data = {"claude": {"windows": [], "err": "", "at": 0, "state": IDLE, "backoff": 0},
                     "codex": {"windows": [], "err": "", "at": 0, "state": IDLE, "backoff": 0}}
        self.spin = 0
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # draw in real pixels, not a blurry stretched bitmap
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", "#010203")  # Windows: everything this colour is see-through
        self.S = self.root.winfo_fpixels('1i') / 96
        self.w, self.h = round(330 * self.S), round(40 * self.S)
        x = self.cfg.get("x", (self.root.winfo_screenwidth() - self.w) // 2)
        self.root.geometry(f"{self.w}x{self.h}+{x}+{self.cfg.get('y', 0)}")
        self.c = tk.Canvas(self.root, width=self.w, height=self.h, bg="#010203", highlightthickness=0)
        self.c.pack()
        self.tip = None
        self.c.bind("<ButtonPress-1>", self.press)
        self.c.bind("<B1-Motion>", self.drag)
        self.c.bind("<ButtonRelease-1>", self.save_pos)
        self.c.bind("<Enter>", self.show_tip)
        self.c.bind("<Leave>", self.hide_tip)
        self.c.bind("<Button-3>", self.menu)
        self.c.bind("<Double-Button-1>", lambda _: self.poll_usage(loop=False))
        self.root.after(100, self.poll_usage)
        self.root.after(200, self.poll_state)
        self.draw()

    # usage and state polling
    def poll_usage(self, loop=True):
        # Never lose the schedule: whatever goes wrong is recorded in err and the re-arm always runs.
        try:
            self._poll_usage(loop)
        except Exception as e:
            self.data["claude"]["err"] = f"poll failed: {type(e).__name__}"[:60]
        finally:
            if loop:
                self.root.after(USAGE_EVERY * 1000, self.poll_usage)

    def _poll_usage(self, loop):
        now = time.time()
        for name, fetch in (("claude", fetch_claude), ("codex", fetch_codex)):
            d = self.data[name]
            if now < d["backoff"] and loop:
                continue
            try:
                d["windows"], d["err"], d["at"] = fetch(), "", now
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = max(60, int(e.headers.get("Retry-After") or 0))
                    d["backoff"], d["err"] = now + wait, f"rate limited, retry in {wait // 60} min"
                else:
                    d["err"] = "sign in again" if e.code in (401, 403) else f"HTTP {e.code}"
            except Exception as e:  # network, parse or credentials trouble: show it, keep polling
                d["err"] = (str(e) or type(e).__name__)[:60]
            if name == "codex" and not d["windows"]:
                log = newest_codex_log()
                if log:
                    try:
                        d["windows"] = codex_log_usage(read_tail(log))
                        d["err"] = d["err"] or "from last session log"
                    except OSError:
                        pass
        self.draw()
        # Other local tools may read this file: percentages and reset times only, never tokens.
        _write_json(STATE_DIR / "usage.json", {name: {"windows": d["windows"], "err": d["err"], "at": d["at"]} for name, d in self.data.items()})

    def poll_state(self):
        try:
            self.data["claude"]["state"] = claude_state(_read_json(CLAUDE_STATE, {}))
            log = newest_codex_log()
            self.data["codex"]["state"] = codex_state_from_tail(read_tail(log)) if log else IDLE
            self.spin = (self.spin + 20) % 360
            self.draw()
        except Exception:
            pass  # a log vanishing mid-read must not stop the loop
        finally:
            self.root.after(STATE_EVERY * 1000 if not any(d["state"] == WORKING for d in self.data.values()) else 120, self.poll_state)

    # drawing
    def fonts(self):
        from tkinter import font as tkf
        if not hasattr(self, "fn"):
            self.fn = {k: tkf.Font(root=self.root, family=f, size=z) for k, (f, z) in FONTS.items()}
        return self.fn

    def txt(self, x, base, text, key, fill, anchor="w"):
        """Place text by BASELINE so names and percents of different sizes sit on one line."""
        f = self.fonts()[key]
        self.c.create_text(x, base - f.metrics("ascent"), text=text, anchor="n" + anchor, fill=fill, font=f)
        return f.measure(text)

    def draw(self):
        # light frosted pill: ring left, percent big over a small name; reads like a system widget
        c, S, w, h = self.c, self.S, self.w, self.h
        c.delete("all")
        pill(c, w, h, BG, EDGE)
        claude, codex = self.data["claude"], self.data["codex"]
        fable = [win for win in claude["windows"] if win[0] == FABLE]
        segments = (("Claude", claude_ring(claude["windows"]), claude["state"], claude["err"]), ("Fable", fable, IDLE, claude["err"]),
                    ("Codex", codex["windows"], codex["state"], codex["err"]))
        col = (w - 20 * S) / 3
        for i, (title, windows, state, err) in enumerate(segments):
            x0 = 20 * S + i * col
            pct = max((p for _, p, _ in windows), default=0)
            colour = ring_colour(pct, state)
            ring(c, x0 + 11 * S, h / 2, 10 * S, max(3, round(3.2 * S)), pct, state, self.spin, colour, TRACK, BG)
            self.txt(x0 + 30 * S, h / 2 + 1 * S, f"{pct:.0f}%" if windows else "--", "pct", TEXT if not err else MUTED)
            self.txt(x0 + 30 * S, h / 2 + 11 * S, title, "name", MUTED)

    def tip_text(self) -> str:
        lines = []
        for name, title in (("claude", "Claude Code"), ("codex", "Codex")):
            d = self.data[name]
            lines.append(f"{title}: {d['state']}")
            for label, pct, reset in d["windows"]:
                lines.append(f"   {label}: {pct:.0f}% used  {reset_text(reset)}")
            if d["err"]:
                lines.append(f"   ({d['err']})")
            if d["at"] and time.time() - d["at"] > 5 * USAGE_EVERY:
                lines.append(f"   (stale, {round((time.time() - d['at']) / 60)} min old)")
        lines.append("double-click: refresh   right-click: refresh / quit")
        return "\n".join(lines)

    # mouse
    def show_tip(self, _=None):
        self.hide_tip()
        self.tip = self.tk.Toplevel(self.root)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip.geometry(f"+{self.root.winfo_x()}+{self.root.winfo_y() + self.h + 4}")
        self.tk.Label(self.tip, text=self.tip_text(), justify="left", bg=BG, fg=TEXT, font=("Segoe UI", 9), padx=12, pady=8,
                      highlightthickness=1, highlightbackground=EDGE).pack()

    def hide_tip(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def press(self, e):
        self.hide_tip()
        self.drag_from = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag(self, e):
        self.root.geometry(f"+{e.x_root - self.drag_from[0]}+{e.y_root - self.drag_from[1]}")

    def save_pos(self, _=None):
        self.cfg.update(x=self.root.winfo_x(), y=self.root.winfo_y())
        _write_json(CONFIG, self.cfg)

    def menu(self, e):
        m = self.tk.Menu(self.root, tearoff=0)
        m.add_command(label="Refresh now", command=lambda: self.poll_usage(loop=False))
        m.add_command(label="Quit", command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    def quit(self):
        release_instance()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ---------- entry points ----------

_MUTEX = None


def claim_instance(name="NotchWidget") -> bool:
    """True if we are the only Notch. A named Windows mutex (stdlib ctypes) is used rather than a lock file because
    the OS drops it when the process dies, so a crash never leaves a stale lock; works under pythonw."""
    import ctypes
    global _MUTEX
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def release_instance():
    import ctypes
    global _MUTEX
    if _MUTEX:
        ctypes.windll.kernel32.CloseHandle(_MUTEX)
        _MUTEX = None


def install_hooks(remove=False):
    try:  # strict read: a settings file we cannot parse is left alone, never replaced by {} plus our hooks
        settings = json.loads(CLAUDE_SETTINGS.read_text("utf-8")) if CLAUDE_SETTINGS.exists() else {}
    except ValueError:
        print(f"{CLAUDE_SETTINGS} is not valid JSON; fix it first (Notch will not overwrite it)")
        return 1
    settings = remove_hooks(settings) if remove else merge_hooks(settings, sys.executable)
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2), "utf-8")
    print(("removed" if remove else "installed") + f" Notch hooks in {CLAUDE_SETTINGS} (takes effect in new Claude Code sessions)")


def selftest():
    now = 1_800_000_000
    c = parse_claude({"limits": [{"kind": "five_hour", "percent": 42.5, "resets_at": "2027-01-15T10:00:00Z"}],
                      "five_hour": {"utilization": 99, "resets_at": "2027-01-15T10:00:00Z"}, "seven_day": {"utilization": 7, "resets_at": 1800003600}})
    assert c == [("Session (5h)", 42.5, _epoch("2027-01-15T10:00:00Z")), ("Weekly", 7.0, 1800003600)], c
    assert claude_ring(c + [(FABLE, 30.0, None), ("Weekly", 90.0, None)]) == [c[0]]  # session wins even when weekly is fuller
    assert claude_ring([("Weekly", 7.0, None), (FABLE, 30.0, None)]) == [("Weekly", 7.0, None)]
    f = parse_claude({"limits": [{"kind": "weekly_scoped", "percent": 25, "resets_at": 1800003600, "scope": {"model": {"display_name": "Fable"}}},
                                 {"kind": "weekly_scoped", "percent": 1, "resets_at": 1800003600, "scope": None}]})
    assert f == [(FABLE, 25.0, 1800003600), ("Weekly (model-scoped)", 1.0, 1800003600)], f
    x = parse_codex({"rate_limit": {"primary_window": {"used_percent": 12, "limit_window_seconds": 18000, "reset_at": 1800001000},
                                    "secondary_window": {"used_percent": 3.5, "limit_window_seconds": 604800, "reset_after_seconds": 100}}})
    assert [(l, p) for l, p, _ in x] == [("Session (5h)", 12.0), ("Weekly", 3.5)] and x[1][2] > time.time(), x
    log = ('{"timestamp":"2027-01-01T00:00:00Z","type":"event_msg","payload":{"type":"token_count","rate_limits":{'
           '"primary":{"used_percent":55,"window_minutes":300,"resets_at":1800002000},"secondary":null}}}\n')
    assert codex_log_usage(log) == [("Session (5h)", 55.0, 1800002000)]
    t = datetime.fromtimestamp(now, timezone.utc).isoformat()
    tool = f'{{"timestamp":"{t}","type":"response_item","payload":{{"type":"function_call"}}}}'
    said = f'{{"timestamp":"{t}","type":"response_item","payload":{{"type":"message","role":"assistant"}}}}'
    done = f'{{"timestamp":"{t}","type":"event_msg","payload":{{"type":"task_complete"}}}}'
    junk = f'{{"timestamp":"{t}","type":"event_msg","payload":{{"type":"token_count"}}}}'
    assert codex_state_from_tail(tool + "\n" + junk, now + 300) == WORKING
    assert codex_state_from_tail(tool, now + 700) == IDLE
    assert codex_state_from_tail(said, now + 10) == IDLE and codex_state_from_tail(said, now + 2) == WORKING
    assert codex_state_from_tail(done, now) == IDLE and codex_state_from_tail("", now) == IDLE
    s = {"a": {"state": WORKING, "at": now - 30}, "b": {"state": WAITING, "at": now - 30}}
    assert claude_state(s, now) == WAITING
    s["b"]["at"] = now - 700
    assert claude_state(s, now) == WORKING
    s["a"]["state"] = IDLE
    assert claude_state(s, now) == IDLE
    st = merge_hooks({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "theirs"}]}]}}, "py")
    st = merge_hooks(st, "py")  # twice: still one of ours per event
    assert len(st["hooks"]["Stop"]) == 2 and len(st["hooks"]["Notification"]) == 2 and st["hooks"]["Stop"][0]["hooks"][0]["command"] == "theirs"
    assert remove_hooks(st)["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "theirs"}]}]}
    assert reset_text(now + 50 * 60, now) == "resets in 50 min" and reset_text(now + 3 * 3600 + 120, now) == "resets in 3 h 2 min"
    assert ring_colour(95, IDLE) == "#d9363e" and ring_colour(5, WAITING) == "#d69a00"
    import tempfile
    with tempfile.TemporaryDirectory() as td:  # a held target makes os.replace fail: must retry, then give up quietly
        f = Path(td) / "u.json"
        _write_json(f, {"v": 1})
        h = f.open("rb")
        threading.Timer(0.1, h.close).start()
        _write_json(f, {"v": 2})  # released during the retry wait
        assert json.loads(f.read_text("utf-8")) == {"v": 2}
        h = f.open("rb")
        _write_json(f, {"v": 3})  # held throughout: no raise, old content kept, no orphan temp
        h.close()
        assert json.loads(f.read_text("utf-8")) == {"v": 2} and [p.name for p in Path(td).iterdir()] == ["u.json"]
        global CLAUDE_SETTINGS
        real, CLAUDE_SETTINGS = CLAUDE_SETTINGS, Path(td) / "settings.json"
        try:
            CLAUDE_SETTINGS.write_text("{broken", "utf-8")
            assert install_hooks() == 1 and CLAUDE_SETTINGS.read_text("utf-8") == "{broken"
        finally:
            CLAUDE_SETTINGS = real

    class Root:
        def __init__(self): self.after_calls = []
        def after(self, ms, fn): self.after_calls.append(ms)

    class Fake:
        data = {"claude": {"err": ""}}
        root = Root()
        def _poll_usage(self, loop): raise PermissionError("boom")
    Fake.poll_usage = Notch.poll_usage
    fk = Fake()
    fk.poll_usage()
    assert fk.root.after_calls == [USAGE_EVERY * 1000] and "PermissionError" in fk.data["claude"]["err"]
    fk.poll_usage(loop=False)
    assert len(fk.root.after_calls) == 1
    assert claim_instance("NotchSelftest") and not claim_instance("NotchSelftest")
    print("selftest ok")


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if "--hook" in argv:
        event = argv[argv.index("--hook") + 1]
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        record_hook(event, payload)
        return 0
    if "--install-hooks" in argv:
        return install_hooks()
    if "--uninstall-hooks" in argv:
        return install_hooks(remove=True)
    if not claim_instance():
        return 0  # another Notch is already running
    Notch().run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
