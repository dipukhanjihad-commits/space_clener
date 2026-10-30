#!/usr/bin/env python3
"""
C: Starter Cleaner — modern UI edition.
Backend is unchanged from the original design; the UI layer uses customtkinter.
"""
import argparse
import ctypes
import glob
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

try:
    import psutil
except ImportError:
    psutil = None

try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except ImportError:
    HAVE_TRAY = False


# ---- constants -------------------------------------------------------------

APP_NAME = "CStarterCleaner"
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
CONFIG_DIR = os.path.join(LOCALAPPDATA, APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
LOG_FILE = os.path.join(CONFIG_DIR, "cleanup.log")

STARTUP_DIR = os.path.join(
    os.environ.get("APPDATA", ""), "Microsoft", "Windows",
    "Start Menu", "Programs", "Startup",
)
STARTUP_VBS = os.path.join(STARTUP_DIR, "CStarterCleaner.vbs")

STARTUP_DELAY_SEC = 150
DRY_RUN_FIRST_N = 3
FAILURE_ABORT_RATIO = 0.20
PER_RUN_CAP_BYTES = 20 * 1024 ** 3


# ---- theme palette (light, dark) — customtkinter handles auto-switching ----

COLOR_BG          = ("#f4f5f7", "#131418")
COLOR_CARD        = ("#ffffff", "#1e1f24")
COLOR_CARD_HOVER  = ("#f0f2f5", "#262830")
COLOR_BORDER      = ("#e5e7eb", "#2b2d35")
COLOR_TEXT        = ("#111827", "#f3f4f6")
COLOR_MUTED       = ("#6b7280", "#9ca3af")
COLOR_ACCENT      = ("#2563eb", "#60a5fa")
COLOR_ACCENT_HOV  = ("#1d4ed8", "#3b82f6")

FONT_FAMILY = "Segoe UI"
FONT_HERO   = (FONT_FAMILY, 34, "bold")
FONT_TITLE  = (FONT_FAMILY, 17, "bold")
FONT_BODY   = (FONT_FAMILY, 13)
FONT_SMALL  = (FONT_FAMILY, 11)
FONT_LABEL  = (FONT_FAMILY, 10, "bold")


# ---- logging ---------------------------------------------------------------

os.makedirs(CONFIG_DIR, exist_ok=True)
log = logging.getLogger("c_cleaner")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3,
                               encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_handler)


# ---- item registry ---------------------------------------------------------

@dataclass
class Item:
    id: str
    label: str
    category: str
    tier: str
    patterns: list
    needs_admin: bool = False
    gate_procs: list = field(default_factory=list)
    skip_recent_hours: int = 0
    default_clean: bool = True
    default_auto: bool = True
    note: str = ""


CHROME_BASE  = r"%LOCALAPPDATA%\Google\Chrome\User Data"
EDGE_BASE    = r"%LOCALAPPDATA%\Microsoft\Edge\User Data"
FIREFOX_BASE = r"%LOCALAPPDATA%\Mozilla\Firefox\Profiles"


def _browser_cache_patterns(base: str):
    profiles = ["Default", "Profile *", "Guest Profile", "System Profile"]
    subs = ["Cache", "Code Cache", "GPUCache"]
    return [f"{base}\\{p}\\{s}" for p in profiles for s in subs]


ITEMS = [
    # ---- Tier A ----
    Item("user_temp", "User temp files", "System", "auto",
         [r"%TEMP%"], skip_recent_hours=24),
    Item("win_temp", "Windows temp files", "System", "auto",
         [r"%SystemRoot%\Temp"], needs_admin=True, skip_recent_hours=24),
    Item("wer_user", "Windows Error Reporting (user)", "System", "auto",
         [r"%LOCALAPPDATA%\Microsoft\Windows\WER"]),
    Item("wer_programdata", "Windows Error Reporting (system)", "System", "auto",
         [r"%ProgramData%\Microsoft\Windows\WER"], needs_admin=True),
    Item("crash_dumps", "Crash dumps", "System", "auto",
         [r"%LOCALAPPDATA%\CrashDumps"]),
    Item("minidumps", "Kernel minidumps", "System", "auto",
         [r"%SystemRoot%\Minidump"], needs_admin=True),
    Item("edge_cache", "Edge browser cache", "Browsers", "auto",
         _browser_cache_patterns(EDGE_BASE), gate_procs=["msedge.exe"]),
    Item("chrome_cache", "Chrome browser cache", "Browsers", "auto",
         _browser_cache_patterns(CHROME_BASE), gate_procs=["chrome.exe"]),
    Item("firefox_cache", "Firefox browser cache", "Browsers", "auto",
         [f"{FIREFOX_BASE}\\*\\cache2"], gate_procs=["firefox.exe"]),
    Item("firefox_startupcache", "Firefox startup cache", "Browsers", "auto",
         [f"{FIREFOX_BASE}\\*\\startupCache"], gate_procs=["firefox.exe"],
         default_auto=False),
    # ---- Tier B ----
    Item("wu_download", "Windows Update download cache", "Windows", "manual",
         [r"%SystemRoot%\SoftwareDistribution\Download"], needs_admin=True,
         default_auto=False, note="Requires stopping wuauserv & bits"),
    Item("delivery_opt", "Delivery Optimization cache", "Windows", "manual",
         [r"%SystemRoot%\SoftwareDistribution\DeliveryOptimization"],
         needs_admin=True, default_auto=False),
    Item("thumbs", "Thumbnail cache", "Explorer", "manual",
         [r"%LOCALAPPDATA%\Microsoft\Windows\Explorer\thumbcache_*.db"],
         default_auto=False, note="Explorer holds these; may skip some"),
    Item("inetcache", "Explorer web cache (INetCache)", "Explorer", "manual",
         [r"%LOCALAPPDATA%\Microsoft\Windows\INetCache"],
         default_auto=False, note="Explorer holds these; may skip some"),
]

ITEM_BY_ID = {it.id: it for it in ITEMS}


# ---- helpers ---------------------------------------------------------------

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.2f} {unit}"
    return f"{n:.2f} PB"


def expand_patterns(patterns):
    out = []
    for p in patterns:
        expanded = os.path.expandvars(p)
        out.extend(glob.glob(expanded))
    seen, result = set(), []
    for p in out:
        key = os.path.normcase(p)
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def process_running(names):
    if not names or psutil is None:
        return False
    wanted = {n.lower() for n in names}
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() in wanted:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def current_boot_id() -> float:
    if psutil is not None:
        try:
            return psutil.boot_time()
        except Exception:
            pass
    try:
        ticks_ms = ctypes.windll.kernel32.GetTickCount64()
        return time.time() - ticks_ms / 1000.0
    except Exception:
        return 0.0


# ---- config ----------------------------------------------------------------

DEFAULT_CONFIG = {
    "clean_selected": [it.id for it in ITEMS if it.default_clean],
    "auto_selected": [it.id for it in ITEMS if it.tier == "auto" and it.default_auto],
    "start_with_windows": False,
    "theme": "system",
    "dry_runs_remaining": DRY_RUN_FIRST_N,
    "last_auto_boot": 0.0,
    "last_auto_ts": 0.0,
    "last_auto_freed": 0,
    "disabled_after_errors": [],
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k in DEFAULT_CONFIG:
                if k in loaded:
                    cfg[k] = loaded[k]
        except Exception as e:
            log.warning("config load failed: %s", e)
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ---- scanning / cleaning ---------------------------------------------------

def scan_item(item: Item):
    total, count = 0, 0
    for root in expand_patterns(item.patterns):
        if os.path.isfile(root):
            try:
                total += os.path.getsize(root); count += 1
            except OSError:
                pass
        elif os.path.isdir(root):
            for dirpath, _, files in os.walk(root):
                for name in files:
                    try:
                        st = os.stat(os.path.join(dirpath, name))
                        total += st.st_size; count += 1
                    except OSError:
                        pass
    return total, count


@dataclass
class CleanResult:
    freed: int = 0
    deleted: int = 0
    skipped_recent: int = 0
    failed: int = 0
    gated: bool = False
    aborted: bool = False
    errors: list = field(default_factory=list)


def clean_item(item: Item, dry_run: bool = False) -> CleanResult:
    res = CleanResult()
    if item.gate_procs and process_running(item.gate_procs):
        res.gated = True
        log.info("%s: skipped, %s running", item.id, item.gate_procs)
        return res
    threshold = (time.time() - item.skip_recent_hours * 3600
                 if item.skip_recent_hours else 0)
    for root in expand_patterns(item.patterns):
        if not os.path.exists(root):
            continue
        targets = []
        if os.path.isfile(root):
            targets.append(root)
        else:
            for dirpath, _, files in os.walk(root):
                for name in files:
                    targets.append(os.path.join(dirpath, name))
        approx = 0
        for t in targets:
            try:
                approx += os.path.getsize(t)
            except OSError:
                pass
        if approx > PER_RUN_CAP_BYTES:
            res.aborted = True
            log.warning("%s: aborted, %s over cap", item.id, human_size(approx))
            res.errors.append(f"aborted: {human_size(approx)} over cap")
            return res
        for path in targets:
            try:
                st = os.stat(path)
            except OSError:
                res.failed += 1; continue
            if threshold and st.st_mtime > threshold:
                res.skipped_recent += 1; continue
            if dry_run:
                res.freed += st.st_size; res.deleted += 1; continue
            try:
                size = st.st_size
                os.remove(path)
                res.freed += size; res.deleted += 1
            except OSError as e:
                res.failed += 1
                if len(res.errors) < 10:
                    res.errors.append(f"{path}: {e}")
        if os.path.isdir(root) and not dry_run:
            for dirpath, _, _ in os.walk(root, topdown=False):
                if os.path.normcase(dirpath) == os.path.normcase(root):
                    continue
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    pass
    ops = res.deleted + res.failed
    if ops and (res.failed / ops) > FAILURE_ABORT_RATIO:
        log.warning("%s: high failure rate %d/%d", item.id, res.failed, ops)
    return res


# ---- startup shortcut ------------------------------------------------------

def startup_enabled() -> bool:
    return os.path.exists(STARTUP_VBS)


def enable_startup() -> None:
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, "--startup"
    else:
        exe = sys.executable
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pythonw):
            exe = pythonw
        args = f'"{os.path.abspath(__file__)}" --startup'
    cmd = f'"{exe}" {args}'
    escaped = cmd.replace('"', '""')
    vbs = ('Set WshShell = CreateObject("WScript.Shell")\r\n'
           f'WshShell.Run "{escaped}", 0, False\r\n')
    os.makedirs(STARTUP_DIR, exist_ok=True)
    with open(STARTUP_VBS, "w", encoding="utf-8") as f:
        f.write(vbs)
    log.info("startup entry created: %s", STARTUP_VBS)


def disable_startup() -> None:
    for path in (STARTUP_VBS, STARTUP_VBS.replace(".vbs", ".cmd")):
        try:
            if os.path.exists(path):
                os.remove(path)
                log.info("startup entry removed: %s", path)
        except OSError as e:
            log.warning("could not remove %s: %s", path, e)


# ---- UI --------------------------------------------------------------------

class App:
    def __init__(self, root: ctk.CTk, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.is_admin = is_admin()
        self.rows = {}
        self.scan_sizes = {}
        self.scanning = False
        self.cleaning = False
        self.tray = None

        root.title("C: Starter Cleaner")
        root.geometry("720x740")
        root.minsize(660, 640)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._refresh_total_label()
        self._update_status_bar()

    # ----- layout ---------------------------------------------------------

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(22, 10))

        ctk.CTkLabel(header, text="C: Starter Cleaner",
                     font=FONT_TITLE, text_color=COLOR_TEXT,
                     anchor="w").pack(side="left")

        current_theme = self.cfg.get("theme", "system").capitalize()
        if current_theme not in ("System", "Light", "Dark"):
            current_theme = "System"
        self.theme_var = tk.StringVar(value=current_theme)
        self.theme_seg = ctk.CTkSegmentedButton(
            header,
            values=["System", "Light", "Dark"],
            variable=self.theme_var,
            command=self._on_theme_change,
            font=FONT_SMALL,
            height=30,
            corner_radius=8,
            fg_color=COLOR_BG,                      # was "transparent" -> crash
            selected_color=COLOR_ACCENT,
            selected_hover_color=COLOR_ACCENT_HOV,
            unselected_color=COLOR_CARD,
            unselected_hover_color=COLOR_CARD_HOVER,
            text_color=COLOR_TEXT,
        )
        self.theme_seg.pack(side="right")

        # Hero card
        hero = ctk.CTkFrame(self.root, fg_color=COLOR_CARD, corner_radius=16,
                            border_width=1, border_color=COLOR_BORDER,
                            height=124)
        hero.pack(fill="x", padx=24, pady=(4, 16))
        hero.pack_propagate(False)

        ctk.CTkLabel(hero, text="SPACE TO BE FREED", font=FONT_LABEL,
                     text_color=COLOR_MUTED, anchor="w").place(
                         relx=0.045, rely=0.26, anchor="w")
        self.total_label = ctk.CTkLabel(hero, text="—", font=FONT_HERO,
                                        text_color=COLOR_ACCENT, anchor="w")
        self.total_label.place(relx=0.045, rely=0.62, anchor="w")
        self.hero_hint = ctk.CTkLabel(
            hero, text="Run Scan to see what can be freed",
            font=FONT_SMALL, text_color=COLOR_MUTED)
        self.hero_hint.place(relx=0.955, rely=0.5, anchor="e")

        # Action bar
        actions = ctk.CTkFrame(self.root, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(0, 12))

        self.scan_btn = ctk.CTkButton(
            actions, text="Scan", command=self.do_scan,
            width=110, height=38, corner_radius=10, font=FONT_BODY,
            fg_color=COLOR_CARD, hover_color=COLOR_CARD_HOVER,
            text_color=COLOR_TEXT, border_width=1, border_color=COLOR_BORDER,
        )
        self.scan_btn.pack(side="left")

        self.clean_btn = ctk.CTkButton(
            actions, text="Clean Selected", command=self.do_clean,
            width=160, height=38, corner_radius=10, font=FONT_BODY,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOV,
            text_color="#ffffff",
        )
        self.clean_btn.pack(side="left", padx=8)

        ctk.CTkLabel(actions, text="⚡  auto-clean at startup (Tier A only)",
                     font=FONT_SMALL, text_color=COLOR_MUTED
                     ).pack(side="right")

        # Scrollable body
        self.scroll = ctk.CTkScrollableFrame(
            self.root, fg_color="transparent",
            scrollbar_button_color=COLOR_BORDER,
            scrollbar_button_hover_color=COLOR_MUTED,
        )
        self.scroll.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self._build_rows(self.scroll)

        # Footer switches
        footer = ctk.CTkFrame(self.root, fg_color=COLOR_CARD, corner_radius=12,
                              border_width=1, border_color=COLOR_BORDER,
                              height=56)
        footer.pack(fill="x", padx=24, pady=(0, 8))
        footer.pack_propagate(False)

        self.start_var = tk.BooleanVar(
            value=self.cfg.get("start_with_windows", False))
        ctk.CTkSwitch(
            footer, text="Start with Windows", variable=self.start_var,
            command=self._toggle_startup, font=FONT_BODY,
            text_color=COLOR_TEXT, progress_color=COLOR_ACCENT,
            button_color=("#ffffff", "#ffffff"),
            button_hover_color=("#f0f0f0", "#f0f0f0"),
            fg_color=COLOR_BORDER,
        ).pack(side="left", padx=16, pady=16)

        self.auto_on_startup_var = tk.BooleanVar(
            value=bool(self.cfg.get("auto_selected")))
        ctk.CTkSwitch(
            footer, text="Auto-clean ⚡ items at startup",
            variable=self.auto_on_startup_var,
            command=self._toggle_auto_clean,
            font=FONT_BODY, text_color=COLOR_TEXT,
            progress_color=COLOR_ACCENT,
            button_color=("#ffffff", "#ffffff"),
            button_hover_color=("#f0f0f0", "#f0f0f0"),
            fg_color=COLOR_BORDER,
        ).pack(side="left", padx=16, pady=16)

        # Status bar
        status_frame = ctk.CTkFrame(self.root, fg_color=COLOR_CARD,
                                    corner_radius=10, border_width=1,
                                    border_color=COLOR_BORDER, height=34)
        status_frame.pack(fill="x", padx=24, pady=(0, 18))
        status_frame.pack_propagate(False)
        self.status_var = tk.StringVar(value="Ready.")
        ctk.CTkLabel(status_frame, textvariable=self.status_var,
                     font=FONT_SMALL, text_color=COLOR_MUTED, anchor="w"
                     ).pack(fill="x", padx=14, pady=7)

    def _build_rows(self, parent):
        current_cat = None
        for item in ITEMS:
            if item.category != current_cat:
                current_cat = item.category
                ctk.CTkLabel(parent, text=current_cat.upper(),
                             font=FONT_LABEL, text_color=COLOR_MUTED,
                             anchor="w").pack(fill="x", padx=10, pady=(16, 4))

            row = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=10,
                               border_width=1, border_color=COLOR_BORDER,
                               height=54)
            row.pack(fill="x", pady=3, padx=2)
            row.pack_propagate(False)

            clean_var = tk.BooleanVar(
                value=item.id in self.cfg.get("clean_selected", []))
            auto_var = tk.BooleanVar(
                value=item.id in self.cfg.get("auto_selected", []))

            ctk.CTkCheckBox(
                row, text="", variable=clean_var,
                command=self._refresh_total_label,
                width=24, checkbox_width=20, checkbox_height=20,
                corner_radius=6, border_width=2,
                fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOV,
                border_color=COLOR_MUTED,
            ).pack(side="left", padx=(16, 10), pady=16)

            if item.tier == "auto":
                ctk.CTkSwitch(
                    row, text="", variable=auto_var,
                    command=self._save_selections,
                    switch_width=34, switch_height=18,
                    progress_color=COLOR_ACCENT,
                    button_color=("#ffffff", "#ffffff"),
                    button_hover_color=("#f0f0f0", "#f0f0f0"),
                    fg_color=COLOR_BORDER,
                ).pack(side="left", padx=(0, 14), pady=18)
            else:
                ctk.CTkLabel(row, text="—", font=FONT_SMALL,
                             text_color=COLOR_MUTED, width=34
                             ).pack(side="left", padx=(0, 14), pady=18)

            label = item.label
            if item.needs_admin and not self.is_admin:
                label += "  ·  admin required"
            ctk.CTkLabel(row, text=label, anchor="w", font=FONT_BODY,
                         text_color=COLOR_TEXT
                         ).pack(side="left", fill="x", expand=True, pady=16)

            size_lbl = ctk.CTkLabel(row, text="—", width=110, anchor="e",
                                    font=FONT_BODY, text_color=COLOR_MUTED)
            size_lbl.pack(side="right", padx=(0, 16), pady=16)

            self.rows[item.id] = {
                "item": item, "clean_var": clean_var,
                "auto_var": auto_var, "size_label": size_lbl,
            }

    # ----- theme + persistence -------------------------------------------

    def _on_theme_change(self, value: str):
        mode = value.lower()
        ctk.set_appearance_mode(mode)
        try:
            self.root.configure(fg_color=COLOR_BG)
        except Exception:
            pass
        self.cfg["theme"] = mode
        save_config(self.cfg)

    def _selected_ids(self, key):
        return [iid for iid, r in self.rows.items() if r[key].get()]

    def _save_selections(self):
        self.cfg["clean_selected"] = self._selected_ids("clean_var")
        self.cfg["auto_selected"] = [
            i for i in self._selected_ids("auto_var")
            if ITEM_BY_ID[i].tier == "auto"
        ]
        save_config(self.cfg)
        self._refresh_total_label()

    def _refresh_total_label(self):
        total = 0
        for iid, row in self.rows.items():
            if row["clean_var"].get() and iid in self.scan_sizes:
                s = self.scan_sizes[iid][0]
                if s > 0:
                    total += s
        if not self.scan_sizes:
            self.total_label.configure(text="—")
            self.hero_hint.configure(
                text="Run Scan to see what can be freed")
        else:
            self.total_label.configure(text=human_size(total))
            n = sum(1 for r in self.rows.values() if r["clean_var"].get())
            self.hero_hint.configure(
                text=f"{n} item{'s' if n != 1 else ''} selected")

    def _update_status_bar(self):
        ts = self.cfg.get("last_auto_ts", 0)
        if ts:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            freed = human_size(self.cfg.get("last_auto_freed", 0))
            self.status_var.set(f"Last auto-clean: {when} · freed {freed}")
        else:
            self.status_var.set(
                "Ready. Run Scan to see what can be freed.")

    # ----- actions --------------------------------------------------------

    def do_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.scan_btn.configure(state="disabled")
        self.status_var.set("Scanning…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        results = {}
        for item in ITEMS:
            if item.needs_admin and not self.is_admin:
                results[item.id] = (-1, 0); continue
            try:
                results[item.id] = scan_item(item)
            except Exception as e:
                log.warning("scan %s failed: %s", item.id, e)
                results[item.id] = (0, 0)
        self.root.after(0, self._scan_done, results)

    def _scan_done(self, results):
        self.scan_sizes = results
        for iid, (size, _c) in results.items():
            lbl = self.rows[iid]["size_label"]
            if size == -1:
                lbl.configure(text="(admin)")
            else:
                lbl.configure(text=human_size(size) if size else "0 B")
        self.scanning = False
        self.scan_btn.configure(state="normal")
        self._refresh_total_label()
        self.status_var.set("Scan complete.")

    def do_clean(self):
        if self.cleaning:
            return
        selected = [ITEM_BY_ID[i] for i in self._selected_ids("clean_var")]
        if not selected:
            messagebox.showinfo("Nothing selected",
                                "Tick at least one item to clean.")
            return
        if not messagebox.askyesno(
            "Confirm clean",
            f"Clean {len(selected)} selected item(s)?\n\n"
            "Deletion is permanent. Continue?"):
            return
        self.cleaning = True
        self.clean_btn.configure(state="disabled")
        self.status_var.set("Cleaning…")
        threading.Thread(target=self._clean_worker, args=(selected,),
                         daemon=True).start()

    def _clean_worker(self, items):
        total_freed, messages = 0, []
        for item in items:
            if item.needs_admin and not self.is_admin:
                messages.append(f"{item.label}: skipped (admin)"); continue
            res = clean_item(item)
            total_freed += res.freed
            if res.gated:
                messages.append(f"{item.label}: skipped (process running)")
            elif res.aborted:
                messages.append(f"{item.label}: aborted (over cap)")
            else:
                messages.append(
                    f"{item.label}: freed {human_size(res.freed)} "
                    f"({res.deleted} files, {res.failed} failed)")
            log.info("clean %s: freed=%d deleted=%d skipped=%d failed=%d",
                     item.id, res.freed, res.deleted,
                     res.skipped_recent, res.failed)
        self.root.after(0, self._clean_done, total_freed, messages)

    def _clean_done(self, total_freed, messages):
        self.cleaning = False
        self.clean_btn.configure(state="normal")
        self.status_var.set(
            f"Clean complete. Freed {human_size(total_freed)}.")
        self._refresh_total_label()
        body = "\n".join(messages[:20])
        if len(messages) > 20:
            body += f"\n… and {len(messages) - 20} more"
        messagebox.showinfo(
            "Clean complete",
            f"Freed {human_size(total_freed)}.\n\n{body}")

    def _toggle_startup(self):
        try:
            if self.start_var.get():
                enable_startup()
            else:
                disable_startup()
            self.cfg["start_with_windows"] = self.start_var.get()
            save_config(self.cfg)
        except Exception as e:
            log.exception("startup toggle failed")
            messagebox.showerror(
                "Error", f"Could not update startup entry:\n{e}")
            self.start_var.set(startup_enabled())

    def _toggle_auto_clean(self):
        self._save_selections()

    # ----- tray / lifecycle ----------------------------------------------

    def _on_close(self):
        if HAVE_TRAY:
            self.root.withdraw()
            self.status_var.set("Minimized to tray.")
        else:
            self.root.destroy()

    def show_window(self):
        self.root.after(0, self._restore)

    def _restore(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def run_tray(self):
        if not HAVE_TRAY:
            return
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((6, 6, 58, 58), fill=(37, 99, 235, 255))
        draw.text((22, 18), "C:", fill=(255, 255, 255, 255))

        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda: self.show_window(), default=True),
            pystray.MenuItem("Scan now",
                             lambda: self.root.after(0, self.do_scan)),
            pystray.MenuItem("Clean selected",
                             lambda: self.root.after(0, self.do_clean)),
            pystray.MenuItem("Run auto-clean now",
                             lambda: self._tray_auto_clean()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",
                             lambda: self.root.after(0, self.root.destroy)),
        )
        self.tray = pystray.Icon(
            "c_cleaner", image, "C: Starter Cleaner", menu)
        self.tray.run()

    def _tray_auto_clean(self):
        threading.Thread(target=self._startup_auto_clean,
                         kwargs={"respect_schedule": False},
                         daemon=True).start()

    # ----- startup auto-clean --------------------------------------------

    def startup_delayed_clean(self):
        log.info("startup mode: waiting %ds before auto-clean",
                 STARTUP_DELAY_SEC)
        time.sleep(STARTUP_DELAY_SEC)
        self._startup_auto_clean(respect_schedule=True)

    def _startup_auto_clean(self, respect_schedule):
        cfg = load_config()
        if not cfg.get("auto_selected"):
            log.info("auto-clean: nothing selected, skipping")
            return
        if respect_schedule:
            if abs(current_boot_id() - cfg.get("last_auto_boot", 0)) < 60:
                log.info("auto-clean: already ran this boot, skipping")
                return
        dry = cfg.get("dry_runs_remaining", 0) > 0
        log.info("auto-clean: %s run (%d dry remaining)",
                 "DRY" if dry else "LIVE",
                 cfg.get("dry_runs_remaining", 0))
        selected = [ITEM_BY_ID[i] for i in cfg["auto_selected"]
                    if i in ITEM_BY_ID and ITEM_BY_ID[i].tier == "auto"]
        selected = [it for it in selected
                    if it.id not in cfg.get("disabled_after_errors", [])]
        total_freed = 0
        for item in selected:
            if item.needs_admin and not is_admin():
                log.info("auto-clean: %s skipped (admin)", item.id); continue
            res = clean_item(item, dry_run=dry)
            total_freed += res.freed
            if res.gated:
                log.info("auto-clean: %s gated", item.id)
            if res.aborted:
                log.warning("auto-clean: %s aborted", item.id)
            if res.deleted + res.failed > 50:
                ratio = res.failed / (res.deleted + res.failed)
                if ratio > FAILURE_ABORT_RATIO:
                    cfg.setdefault("disabled_after_errors", []).append(item.id)
                    log.warning("auto-clean: disabling %s (failure rate)",
                                item.id)
        if dry:
            cfg["dry_runs_remaining"] = cfg.get("dry_runs_remaining", 0) - 1
        else:
            cfg["last_auto_freed"] = total_freed
        if respect_schedule:
            cfg["last_auto_boot"] = current_boot_id()
            cfg["last_auto_ts"] = time.time()
        save_config(cfg)
        log.info("auto-clean done: freed %s (dry=%s)",
                 human_size(total_freed), dry)
        if self.tray is not None:
            try:
                self.tray.notify(
                    f"Cleaned {human_size(total_freed)}"
                    + (" (dry run)" if dry else ""),
                    "C: Starter Cleaner")
            except Exception:
                pass
        self.root.after(0, self._update_status_bar)


# ---- entry point -----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="C: Starter Cleaner")
    parser.add_argument("--startup", action="store_true")
    parser.add_argument("--now", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    args = parser.parse_args()

    log.info("==== start (startup=%s, now=%s, admin=%s) ====",
             args.startup, args.now, is_admin())

    cfg = load_config()
    ctk.set_appearance_mode(cfg.get("theme", "system"))
    ctk.set_default_color_theme("blue")

    root = ctk.CTk(fg_color=COLOR_BG)
    app = App(root, cfg)

    if args.startup or args.now:
        root.withdraw()
        if args.now:
            threading.Thread(target=app._startup_auto_clean,
                             kwargs={"respect_schedule": False},
                             daemon=True).start()
        else:
            threading.Thread(target=app.startup_delayed_clean,
                             daemon=True).start()

    if HAVE_TRAY and not args.no_tray:
        threading.Thread(target=app.run_tray, daemon=True).start()
    elif args.startup or args.now:
        def finish():
            time.sleep(1)
            root.after(0, root.destroy)
        threading.Thread(target=finish, daemon=True).start()

    try:
        root.mainloop()
    finally:
        if app.tray is not None:
            try:
                app.tray.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()