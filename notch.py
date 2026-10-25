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


def rshort(epoch, now=None) -> str:
    """Reset time short enough for the pill: 48m, 2h 10m, 3d 4h."""
    left = (epoch or 0) - (now or time.time())
    if not epoch or left <= 0:
        return ""
    m = int(left // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h {m % 60}m" if m < 1440 else f"{m // 1440}d {m % 1440 // 60}h"


ACCENT = "#2f7df6"  # move-mode edge and the lock pulse
# colour name -> (background, edge, ring track, text, muted text, ring under 70%, dark?).
# Rings take the theme's colour until 70%; orange, red and amber stay the same everywhere so warnings still read.
THEMES = {
    "Light": ("#f3f4f6", "#d6d9df", "#dde0e6", "#16171b", "#6a6f7a", "#1fa970", False),
    "Midnight": ("#13151b", "#2c303b", "#262a33", "#f1f3f7", "#8b93a3", "#7aa2ff", True),
    "Navy": ("#0e1420", "#27324a", "#222c42", "#eef2fb", "#7f8ba6", "#4fb3ff", True),
    "Paper": ("#fbfaf7", "#e0dcd2", "#e9e5dc", "#1c1d21", "#8a8578", "#2a8c82", False),
    "Forest": ("#12261d", "#2a4a3a", "#1f3a2d", "#e6efe3", "#9bb8a4", "#9fd8a9", True),
    "Black": ("#000000", "#2a2a2a", "#1c1c1c", "#ffffff", "#8a8a8a", "#38e1ff", True),
    "Glass": ("#5a5f6b", "#7a808d", "#4a4f5a", "#f4f5f8", "#c3c8d2", "#eef1f7", True),
    "Mocha": ("#2b211c", "#4a3a30", "#3a2d25", "#f3e6d3", "#b79f86", "#e6d5bd", True),
    "Purple": ("#1d1233", "#3b2a63", "#2c1f4c", "#f0eaff", "#a798c9", "#b79cff", True),
}
SIDE_SPOTS = True  # False: six spots only (top and bottom rows); True: side middles too, as a vertical column
VCOL = {"Rings": 128, "Number in ring": 156, "Bars": 150}  # column width on a side spot, logical px
GAP = 5  # gap between stacked chips (and the room between stacked segments)
LAYOUTS = {"Rings": (400, 40), "Number in ring": (440, 46), "Bars": (440, 46)}  # logical width x height


def ring_colour(percent: float, state: str, dark=False, ok=None) -> str:
    if state == WAITING:
        return "#ffc233" if dark else "#d69a00"  # amber: it needs you
    if dark:
        return "#ff5c64" if percent >= 90 else "#ff9a3c" if percent >= 70 else ok or "#3ddc97"
    return "#d9363e" if percent >= 90 else "#e57f10" if percent >= 70 else ok or "#1fa970"


def blend(a: str, b: str, t: float) -> str:
    """Colour a mixed toward b by t (0..1): used to fake anti-aliasing with a soft halo."""
    pa, pb = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


SHAPES = ("Pill", "Notch", "Rounded rectangle", "Chips", "None")


def flush_sides(work, x, y, w, h):
    """(top, right, bottom, left): which sides of the window at (x, y) lie against the work area's edges."""
    l, t, r, b = work
    return (y <= t, x + w >= r, y + h >= b, x <= l)


def shape_points(x0, y0, x1, y1, radii, ear=None):
    """Outline of a rectangle with per-corner radii (tl, tr, br, bl). ear=("top"|"bottom", e) swaps the two corners on
    that side for concave fillets that flare out to the screen edge (a notch's "ears"); the body is inset e each side."""
    def arc(cx, cy, r, a0, a1):
        if r < 1:
            return [(cx, cy)]
        n = max(3, int(r // 2))
        return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * k / n)), cy + r * math.sin(math.radians(a0 + (a1 - a0) * k / n)))
                for k in range(n + 1)]
    tl, tr, br, bl = radii
    if ear and ear[0] == "bottom":  # mirror of the top case
        return [(x, y0 + y1 - y) for x, y in shape_points(x0, y0, x1, y1, (bl, br, tr, tl), ("top", ear[1]))]
    if ear:
        e = ear[1]
        left = [(x0 + e * math.sin(math.radians(t)), y0 + e - e * math.cos(math.radians(t))) for t in range(0, 91, 10)]
        return (left + arc(x0 + e + bl, y1 - bl, bl, 180, 90) + arc(x1 - e - br, y1 - br, br, 90, 0)
                + [(x1 - x + x0, y) for x, y in reversed(left)])
    return (arc(x0 + tl, y0 + tl, tl, 180, 270) + arc(x1 - tr, y0 + tr, tr, 270, 360)
            + arc(x1 - br, y1 - br, br, 0, 90) + arc(x0 + bl, y1 - bl, bl, 90, 180))


def backdrop(c, x0, y0, x1, y1, r, flush, fill, edge, ear=None):
    """One shaped background: corners touching a flush side are square, the others keep radius r."""
    t, rt, b, l = flush
    radii = (0 if t or l else r, 0 if t or rt else r, 0 if b or rt else r, 0 if b or l else r)
    c.create_polygon([v for pt in shape_points(x0, y0, x1, y1, radii, ear) for v in pt], fill=fill, outline=edge)
    for on, line in ((t, (x0, y0, x1, y0)), (rt, (x1, y0, x1, y1)), (b, (x0, y1, x1, y1)), (l, (x0, y0, x0, y1))):
        if on:  # no hairline along the screen edge itself
            c.create_line(*line, fill=fill)


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


# ---------- placement: snap spots, easing, work area ----------

def snap_spots(work, w, h, margin):
    """Eight (x, y) top-left spots inside work=(l, t, r, b): corners and edge middles, inset by margin.
    Top-middle sits flush against the top (the classic notch spot)."""
    l, t, r, b = work
    xs, ys = (l + margin, (l + r - w) // 2, r - w - margin), (t + margin, (t + b - h) // 2, b - h - margin)
    return [(x, t if (x, y) == (xs[1], ys[0]) else y) for y in ys for x in xs if (x, y) != (xs[1], ys[1])]


def snap_all(work, hw, hh, vw, vh, sides=True):
    """Spots as (x, y, w, h, vertical): top and bottom rows at the wide size, side middles at the column size."""
    hs, vs = snap_spots(work, hw, hh, 0), snap_spots(work, vw, vh, 0)  # order: TL TM TR ML MR BL BM BR
    return [hs[i] + (hw, hh, False) for i in (0, 1, 2, 5, 6, 7)] + ([vs[i] + (vw, vh, True) for i in (3, 4)] if sides else [])


def nearest_by_centre(spots, cx, cy):
    return min(spots, key=lambda s: (s[0] + s[2] / 2 - cx) ** 2 + (s[1] + s[3] / 2 - cy) ** 2)


def nearest_spot(spots, x, y):
    """The spot closest to the pill's top-left (x, y); same size pill, so this is also nearest by centre."""
    return min(spots, key=lambda s: (s[0] - x) ** 2 + (s[1] - y) ** 2)


def ease_out_back(t: float) -> float:
    """0 -> 1 with a gentle overshoot (about 4%) on the way."""
    c1 = 1.0
    return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2


def work_area(x, y):
    """(l, t, r, b) work area (no taskbar) of the monitor nearest the point (x, y); the primary screen if the call fails."""
    import ctypes
    from ctypes import wintypes as wt

    class Info(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("mon", wt.RECT), ("work", wt.RECT), ("flags", wt.DWORD)]

    u = ctypes.windll.user32
    try:
        u.MonitorFromPoint.argtypes, u.MonitorFromPoint.restype = [wt.POINT, wt.DWORD], ctypes.c_void_p
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(Info)]
        info = Info(ctypes.sizeof(Info))
        u.GetMonitorInfoW(u.MonitorFromPoint(wt.POINT(int(x), int(y)), 2), ctypes.byref(info))  # 2 = DEFAULTTONEAREST
        r = info.work
        if r.right > r.left:
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)


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
        self.layout = self.cfg.get("layout") if self.cfg.get("layout") in LAYOUTS else "Rings"
        self.theme = self.cfg.get("theme") if self.cfg.get("theme") in THEMES else "Light"
        self.w, self.h = round(LAYOUTS[self.layout][0] * self.S), round(LAYOUTS[self.layout][1] * self.S)
        self.shape = self.cfg.get("shape") if self.cfg.get("shape") in SHAPES else "Pill"
        self.margin, self.moving, self.edge = 0, False, THEMES[self.theme][1]  # margin 0: every spot sits flush on its edge
        self.vert = bool(self.cfg.get("vert")) and SIDE_SPOTS
        self.w, self.h = self.dims(self.vert)
        x = self.cfg.get("x", (self.root.winfo_screenwidth() - self.w) // 2)
        y = self.cfg.get("y", 0)
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")
        x, y, self.w, self.h, self.vert = self.spot_for(x + self.w // 2, y + self.h // 2)  # saved spot -> nearest snap spot, no animation
        self.root.geometry(f"{self.w}x{self.h}")
        self.place(x, y)
        self.c = tk.Canvas(self.root, width=self.w, height=self.h, bg="#010203", highlightthickness=0)
        self.c.pack()
        self.tip = None
        self.c.bind("<ButtonPress-1>", self.press)
        self.c.bind("<B1-Motion>", self.drag)
        self.c.bind("<ButtonRelease-1>", self.release)
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
    def txt(self, x, base, text, spec, fill, anchor="w"):
        """Place text by BASELINE so texts of different sizes sit on one line. spec = (family, points)."""
        f = self.font(spec)
        self.c.create_text(x, base - f.metrics("ascent"), text=text, anchor="n" + anchor, fill=fill, font=f)
        return f.measure(text)

    def font(self, spec):
        from tkinter import font as tkf
        self.fn = getattr(self, "fn", {})
        if spec not in self.fn:
            self.fn[spec] = tkf.Font(root=self.root, family=spec[0], size=spec[1])
        return self.fn[spec]

    def segments(self):
        """[(title, percent, short reset, state, has numbers, error)] for Claude, Fable, Codex."""
        claude, codex = self.data["claude"], self.data["codex"]
        fable = [win for win in claude["windows"] if win[0] == FABLE]
        out = []
        for title, windows, state, err in (("Claude", claude_ring(claude["windows"]), claude["state"], claude["err"]),
                                           ("Fable", fable, IDLE, claude["err"]), ("Codex", codex["windows"], codex["state"], codex["err"])):
            top = max(windows, key=lambda win: win[1], default=None)  # the fullest window: its reset is the one shown
            out.append((title, top[1] if top else 0, rshort(top[2]) if top else "", state, bool(windows), err))
        return out

    def draw(self):
        c, S, w, h = self.c, self.S, self.w, self.h
        bg, edge, track, text, muted, ok, dark = THEMES[self.theme]
        c.delete("all")
        px, py = self.pos
        flush = (False,) * 4 if self.moving else flush_sides(work_area(px + w // 2, py + h // 2), px, py, w, h)  # dragging: floats, fully round
        e = 12 * S if self.shape == "Notch" and flush[0] != flush[2] and not (flush[1] or flush[3]) else 0  # ears: top/bottom-middle only
        ear = ("top" if flush[0] else "bottom", e) if e else None
        sh, g, V = LAYOUTS[self.layout][1] * S, GAP * S, self.vert  # one segment's height; V: stacked column on a side spot
        r = {"Pill": sh / 2, "Notch": 15 * S, "Rounded rectangle": 8 * S, "Chips": sh / 2}.get(self.shape, 0)
        if V:  # three boxes stacked top to bottom, full width
            boxes = [(0, i * (sh + g), w) for i in range(3)]
        elif self.shape == "Chips":  # three equal chips side by side
            bw = (w - 2 * g) / 3
            boxes = [(i * (bw + g), 0, bw) for i in range(3)]
        else:  # one background, three equal thirds inside it
            pad = {"Pill": 16, "Notch": 12, "Rounded rectangle": 10}.get(self.shape, 10) * S + e
            boxes = [(pad + i * (w - 2 * pad) / 3, 0, (w - 2 * pad) / 3) for i in range(3)]
        if self.shape == "Chips":  # each chip is a shape of its own; only the chips touching a flush side get square corners
            for i, (bx, by, bw) in enumerate(boxes):
                fl = (flush[0] and (i == 0 or not V), flush[1] and (V or i == 2), flush[2] and (i == 2 or not V), flush[3] and (V or i == 0))
                backdrop(c, bx, by, bx + bw - 1, by + sh - 1, r, fl, bg, self.edge)
        elif self.shape != "None":
            backdrop(c, 0, 0, w - 1, h - 1, r, flush, bg, self.edge, ear)
        segs = self.segments()
        SMALL, SEMI7, SEMI9, SEMI11 = ("Segoe UI", 7), ("Segoe UI Semibold", 7), ("Segoe UI Semibold", 9), ("Segoe UI Semibold", 11)
        m = lambda spec, t: self.font(spec).measure(t)
        pct_text = lambda s_: (f"{s_[1]:.0f}" if s_[4] else "--")
        R = 14.5 * S
        if self.layout == "Rings":  # content width: the widest segment, so every segment sits the same way in its box
            cw = 30 * S + max(max(m(SEMI11, pct_text(s_) + "%"), m(SMALL, s_[0]) + 6 * S + m(SEMI7, s_[2])) for s_ in segs)
        elif self.layout == "Number in ring":
            cw = 2 * R + 9 * S + max(max(m(SEMI9, s_[0]), m(("Segoe UI", 8), "resets " + s_[2])) for s_ in segs)
        for i, ((title, pct, reset, state, has, err), (bx, by, bw_)) in enumerate(zip(segs, boxes)):
            cy = by + sh / 2
            colour, num = ring_colour(pct, state, dark, ok), pct_text(segs[i])
            fg = text if not err else muted
            if self.layout == "Rings":  # ring left; percent over name and reset
                x0 = bx + (bw_ - cw) / 2
                ring(c, x0 + 11 * S, cy, 10 * S, max(3, round(3.2 * S)), pct, state, self.spin, colour, track, bg)
                self.txt(x0 + 30 * S, cy + 1 * S, num + ("%" if has else ""), SEMI11, fg)
                n = self.txt(x0 + 30 * S, cy + 11 * S, title, SMALL, muted)
                self.txt(x0 + 30 * S + n + 6 * S, cy + 11 * S, reset, SEMI7, text)
            elif self.layout == "Number in ring":  # percent inside a bigger ring; name and reset beside it
                x0 = bx + (bw_ - cw) / 2
                ring(c, x0 + R, cy, R, max(3, round(3.4 * S)), pct, state, self.spin, colour, track, bg)
                self.txt(x0 + R, cy + 4.5 * S, num, SEMI9, fg, "")
                tx = x0 + 2 * R + 9 * S
                self.txt(tx, cy - 1 * S, title, SEMI9, text)
                self.txt(tx, cy + 11 * S, "resets " + reset if reset else "", ("Segoe UI", 8), muted)
            else:  # Bars: name and percent on top, a bar and the reset underneath
                x0, x1 = bx + 14 * S, bx + bw_ - 14 * S
                self.txt(x0, cy - 3 * S, title, ("Segoe UI Semibold", 8), muted)
                self.txt(x1, cy - 3 * S, num + ("%" if has else ""), ("Segoe UI Semibold", 12), fg, "e")
                self.bar(x0 + 2 * S, cy + 9 * S, (x1 - x0) - m(SMALL, reset) - 10 * S, pct, state, colour)
                self.txt(x1, cy + 12 * S, reset, SMALL, muted, "e")
            if i and self.shape != "Chips":  # faint divider between segments
                if V:
                    c.create_line(14 * S, by - g / 2, w - 14 * S, by - g / 2, fill=track)
                else:
                    c.create_line(bx, cy - 10 * S, bx, cy + 10 * S, fill=track)

    def bar(self, x, y, length, pct, state, colour):
        bg, track = THEMES[self.theme][0], THEMES[self.theme][2]
        t = max(3, round(4 * self.S))
        self.c.create_line(x, y, x + length, y, fill=track, width=t, capstyle="round")
        if state == WORKING:  # a chunk slides along the bar
            a = self.spin / 360 * length * .7
            seg = (x + a, y, x + a + length * .3, y)
        elif pct:
            seg = (x, y, x + max(length * min(pct, 100) / 100, t), y)
        else:
            return
        self.c.create_line(*seg, fill=blend(colour, bg, .6), width=t + 1, capstyle="round")
        self.c.create_line(*seg, fill=colour, width=t, capstyle="round")

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
        lines.append("double-click: refresh   right-click: move / layout / colour / shape / refresh / quit")
        return "\n".join(lines)

    # mouse
    def show_tip(self, _=None):
        self.hide_tip()
        self.tip = self.tk.Toplevel(self.root)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        bg, edge, _, text, *_ = THEMES[self.theme]
        self.tk.Label(self.tip, text=self.tip_text(), justify="left", bg=bg, fg=text, font=("Segoe UI", 9), padx=12, pady=8,
                      highlightthickness=1, highlightbackground=edge).pack()
        self.tip.update_idletasks()
        tw, th, px, py = self.tip.winfo_reqwidth(), self.tip.winfo_reqheight(), self.root.winfo_x(), self.root.winfo_y()
        l, t, r, b = work_area(px + self.w // 2, py + self.h // 2)
        x = px + self.w - tw if px + self.w // 2 > (l + r) / 2 else px          # right half: right-align to the pill
        y = py - th - 4 if py + self.h // 2 > (t + b) / 2 else py + self.h + 4  # bottom half: above the pill
        self.tip.geometry(f"+{x}+{y}")

    def hide_tip(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None

    # moving: locked by default; right-click > Move, drag, release -> snap, glide, lock
    def place(self, x, y):
        self.pos = x, y  # drawing reads this (flush sides), so it is right before the window is even mapped
        self.root.geometry(f"+{x}+{y}")

    def dims(self, vert):
        """Window size in pixels: the layout's wide size, or on a side spot a column of three stacked segments."""
        W, H = LAYOUTS[self.layout]
        W, H = (VCOL[self.layout], 3 * H + 2 * GAP) if vert else (W, H)
        return round(W * self.S), round(H * self.S)

    def spot_for(self, cx, cy):
        """Nearest snap spot to a window centre: (x, y, w, h, vertical)."""
        (hw, hh), (vw, vh) = self.dims(False), self.dims(True)
        return nearest_by_centre(snap_all(work_area(cx, cy), hw, hh, vw, vh, SIDE_SPOTS), cx, cy)

    def fit(self, w, h, vert):
        self.w, self.h, self.vert = w, h, vert
        self.c.config(width=w, height=h)
        self.root.geometry(f"{w}x{h}")

    def start_move(self):
        self.hide_tip()
        self.moving, self.edge, self.drag_from = True, ACCENT, None
        self.c.config(cursor="fleur")
        self.draw()

    def press(self, e):
        if self.moving:
            self.hide_tip()
            self.drag_from = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag(self, e):
        if self.moving and self.drag_from:
            self.place(e.x_root - self.drag_from[0], e.y_root - self.drag_from[1])

    def release(self, _=None):
        if not self.moving or self.drag_from is None:  # a stray release (e.g. from the menu click) must not end move mode
            return
        self.moving = False
        self.c.config(cursor="")
        x0, y0 = self.root.winfo_x(), self.root.winfo_y()
        x1, y1, w1, h1, v1 = self.spot_for(x0 + self.w // 2, y0 + self.h // 2)
        l, t, r, b = work_area(x1 + w1 // 2, y1 + h1 // 2)
        self.cfg.update(x=x1, y=y1, vert=v1)  # save the spot, not the drop point
        _write_json(CONFIG, self.cfg)
        N = 16  # ~250 ms at 16 ms a frame

        def glide(i):
            if i >= N:
                self.fit(w1, h1, v1)  # switch between wide and column only when it lands
                self.place(x1, y1)
                return pulse(0)
            k = ease_out_back(i / N)  # clamp so the overshoot never leaves the work area
            self.place(min(max(round(x0 + (x1 - x0) * k), l), r - min(self.w, w1)), min(max(round(y0 + (y1 - y0) * k), t), b - min(self.h, h1)))
            self.root.after(16, glide, i + 1)

        def pulse(i):  # ~300 ms: accent fades back to the normal edge
            self.edge = blend(ACCENT, THEMES[self.theme][1], min(i / 18, 1))
            self.draw()
            if i < 18:
                self.root.after(16, pulse, i + 1)

        glide(1)

    def menu(self, e):
        m = self.tk.Menu(self.root, tearoff=0)
        m.add_command(label="Move", command=self.start_move)
        self.pick = {k: self.tk.StringVar(value=getattr(self, k)) for k in ("layout", "theme", "shape")}  # kept alive
        for label, key, names in (("Layout", "layout", LAYOUTS), ("Colour", "theme", THEMES), ("Shape", "shape", SHAPES)):
            sub = self.tk.Menu(m, tearoff=0)
            for n in names:
                sub.add_radiobutton(label=n, variable=self.pick[key], value=n, command=lambda k=key, n=n: self.choose(k, n))
            m.add_cascade(label=label, menu=sub)
        m.add_command(label="Refresh now", command=lambda: self.poll_usage(loop=False))
        m.add_command(label="Quit", command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    def choose(self, key, name):
        """Switch layout, colour or shape now and remember it. A new layout is another size: keep the pill's centre, re-snap."""
        self.cfg[key] = name
        setattr(self, key, name)
        if key == "theme":
            self.edge = THEMES[name][1]
        elif key == "layout":
            x, y, w, h, v = self.spot_for(self.pos[0] + self.w // 2, self.pos[1] + self.h // 2)
            self.fit(w, h, v)
            self.place(x, y)
            self.cfg.update(x=x, y=y, vert=v)
        _write_json(CONFIG, self.cfg)
        self.draw()

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
    work, W, H, M = (0, 0, 1920, 1040), 330, 40, 8
    sp = snap_spots(work, W, H, M)
    assert len(set(sp)) == 8 and all(0 <= x and x + W <= 1920 and 0 <= y and y + H <= 1040 for x, y in sp), sp
    assert (795, 0) in sp and nearest_spot(sp, 900, 5) == (795, 0)  # top-middle is flush against the top
    assert nearest_spot(sp, 1500, 900) == (1582, 992) and nearest_spot(sp, 30, 20) == (8, 8)  # corners
    assert nearest_spot(sp, 780, 950) == (795, 992)  # bottom-middle
    assert ease_out_back(0) == 0 and ease_out_back(1) == 1 and max(ease_out_back(i / 100) for i in range(100)) > 1
    assert ring_colour(95, IDLE) == "#d9363e" and ring_colour(5, WAITING) == "#d69a00"
    assert [rshort(now + s, now) for s in (-5, 2999, 7800, 90000, 3 * 86400 + 4 * 3600 + 9)] == ["", "49m", "2h 10m", "1d 1h", "3d 4h"]
    sp0 = snap_spots(work, W, H, 0)  # margin 0: every spot flush; the flush sides follow
    assert (0, 0) in sp0 and (1590, 1000) in sp0 and (795, 0) in sp0 and (0, 500) in sp0
    assert flush_sides(work, 0, 0, W, H) == (True, False, False, True) and flush_sides(work, 795, 1000, W, H) == (False, False, True, False)
    assert flush_sides(work, 1590, 500, W, H) == (False, True, False, False) and flush_sides(work, 500, 300, W, H) == (False,) * 4
    sp = snap_all(work, W, H, 150, 185, True)  # side middles are columns, flush on their edge; sides off -> six spots
    assert len(sp) == 8 and [s for s in sp if s[4]] == [(0, 427, 150, 185, True), (1770, 427, 150, 185, True)] and len(snap_all(work, W, H, 150, 185, False)) == 6
    assert nearest_by_centre(sp, 40, 520)[4] and not nearest_by_centre(sp, 100, 20)[4]
    pts = shape_points(0, 0, 99, 39, (0, 0, 20, 20), ("top", 8))
    assert pts[0] == (0, 0) and min(x for x, _ in pts) == 0 and max(y for _, y in pts) <= 39 and shape_points(0, 0, 9, 9, (0,) * 4)
    assert "Light" in THEMES and all(re.fullmatch(r"#[0-9a-f]{6}", c) for p in THEMES.values() for c in p[:6])
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
