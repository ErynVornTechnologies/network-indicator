"""Network indicator - two-line live network throughput readout docked to the taskbar.

Shows download and upload rate (Mb/s) stacked in two lines, next to the clock, in the
same spirit as the date/time block. A colour bar on the left carries the link status:
green = traffic flowing, amber = link up but idle, red = no active adapter.

Data source: GetIfTable from iphlpapi.dll (per-interface byte counters, no third-party
packages and no PowerShell spawning). The adapter with the highest link speed among the
up, non-virtual interfaces is treated as the primary uplink, so VPN/WSL/Hyper-V virtual
adapters do not double count the same bytes.

Author: Kamil Nagorski, ErynVorn Technologies
"""

from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont

APP_NAME = "NetworkIndicator"
MUTEX_NAME = "Local\\NetworkIndicator_single_instance"

MAX_INTERFACE_NAME_LEN = 256
MAXLEN_PHYSADDR = 8
MAXLEN_IFDESCR = 256

IF_OPER_STATUS_UP = 4          # legacy MIB_IFROW: 4 = connected, 5 = operational
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131
IF_TYPE_PROP_VIRTUAL = 53

IF_TYPE_NAMES = {
    6: "Ethernet",
    23: "PPP",
    71: "Wi-Fi",
    243: "Wi-Fi",
    244: "Wi-Fi",
}

# Interface descriptions that never represent the physical uplink. NDIS filter layers
# (WFP / QoS / VirtualBox / native Wi-Fi filter) mirror the counters of the adapter they
# sit on, so counting them would multiply the same bytes.
VIRTUAL_TOKENS = (
    "loopback",
    "vethernet",
    "hyper-v",
    "pseudo",
    "teredo",
    "isatap",
    "bluetooth",
    "wi-fi direct",
    "virtual",
    "vpn",
    "tap-",
    "wireguard",
    "tailscale",
    "npcap",
    "wsl",
    "docker",
    "wfp",
    "qos",
    "lightweight",
    "light-weight",
    "ndis",
    "filter",
    "-0000",
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)


class MIB_IFROW(ctypes.Structure):
    _fields_ = [
        ("wszName", wintypes.WCHAR * MAX_INTERFACE_NAME_LEN),
        ("dwIndex", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwMtu", wintypes.DWORD),
        ("dwSpeed", wintypes.DWORD),
        ("dwPhysAddrLen", wintypes.DWORD),
        ("bPhysAddr", ctypes.c_ubyte * MAXLEN_PHYSADDR),
        ("dwAdminStatus", wintypes.DWORD),
        ("dwOperStatus", wintypes.DWORD),
        ("dwLastChange", wintypes.DWORD),
        ("dwInOctets", wintypes.DWORD),
        ("dwInUcastPkts", wintypes.DWORD),
        ("dwInNUcastPkts", wintypes.DWORD),
        ("dwInDiscards", wintypes.DWORD),
        ("dwInErrors", wintypes.DWORD),
        ("dwInUnknownProtos", wintypes.DWORD),
        ("dwOutOctets", wintypes.DWORD),
        ("dwOutUcastPkts", wintypes.DWORD),
        ("dwOutNUcastPkts", wintypes.DWORD),
        ("dwOutDiscards", wintypes.DWORD),
        ("dwOutErrors", wintypes.DWORD),
        ("dwOutQLen", wintypes.DWORD),
        ("dwDescrLen", wintypes.DWORD),
        ("bDescr", ctypes.c_ubyte * MAXLEN_IFDESCR),
    ]


iphlpapi.GetIfTable.argtypes = [
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.POINTER(wintypes.ULONG),
    wintypes.BOOL,
]
iphlpapi.GetIfTable.restype = wintypes.DWORD


def read_interface_rows() -> list[dict]:
    """Return one dict per network interface with counters, type and description."""
    size = wintypes.ULONG(0)
    iphlpapi.GetIfTable(None, ctypes.byref(size), False)
    if not size.value:
        return []

    buf = (ctypes.c_ubyte * size.value)()
    if iphlpapi.GetIfTable(buf, ctypes.byref(size), False) != 0:
        return []

    count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
    if count == 0:
        return []

    rows = (MIB_IFROW * count).from_buffer(buf, 4)  # header dword, then the array
    out = []
    for row in rows:
        descr = (
            bytes(row.bDescr[: row.dwDescrLen])
            .decode("latin-1", "ignore")
            .replace("\x00", "")
            .strip()
        )
        out.append(
            {
                "index": int(row.dwIndex),
                "type": int(row.dwType),
                "speed_bps": int(row.dwSpeed),
                "oper_status": int(row.dwOperStatus),
                "in_octets": int(row.dwInOctets),
                "out_octets": int(row.dwOutOctets),
                "descr": descr,
            }
        )
    return out


def is_physical_uplink(row: dict) -> bool:
    if row["oper_status"] < IF_OPER_STATUS_UP:
        return False
    if row["type"] in (IF_TYPE_SOFTWARE_LOOPBACK, IF_TYPE_TUNNEL, IF_TYPE_PROP_VIRTUAL):
        return False
    descr = row["descr"].lower()
    return not any(token in descr for token in VIRTUAL_TOKENS)


def pick_uplink(rows: list[dict]) -> dict | None:
    """The fastest up, physical interface is the one whose counters we report."""
    candidates = [r for r in rows if is_physical_uplink(r)]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r["speed_bps"])


def wrapped_delta(new: int, old: int) -> int:
    """Counters are 32-bit and wrap; a wrap-inclusive difference stays correct."""
    return (new - old) & 0xFFFFFFFF


def dpi_scale() -> float:
    try:
        return user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def theme_is_light() -> bool:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except Exception:
        return False


def single_instance_guard() -> bool:
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    return ctypes.get_last_error() != 183  # 183 = ALREADY_EXISTS


class NetworkIndicator:
    POLL_MS = 1000
    REPOSITION_EVERY = 5  # ticks

    def __init__(self) -> None:
        self.scale = dpi_scale()
        light = theme_is_light()

        self.bg = "#f3f3f3" if light else "#202020"
        self.fg = "#1a1a1a" if light else "#e8e8e8"
        self.muted = "#5a5a5a" if light else "#a0a0a0"
        self.col_active = "#3fb950"
        self.col_idle = "#d29922"
        self.col_off = "#f85149"

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-toolwindow", True)  # keep it out of alt-tab
        except tk.TclError:
            pass

        font_px = max(9, int(round(12 * self.scale)))
        self.f_num = tkfont.Font(family="Consolas", size=-font_px)
        self.f_unit = tkfont.Font(family="Segoe UI", size=-max(7, int(round(8 * self.scale))))

        wrap = tk.Frame(self.root, bg=self.bg)
        wrap.pack(fill="both", expand=True)

        self.status_bar = tk.Frame(wrap, bg=self.col_off, width=max(3, int(3 * self.scale)))
        self.status_bar.pack(side="left", fill="y", padx=(0, max(3, int(4 * self.scale))))

        lines = tk.Frame(wrap, bg=self.bg)
        lines.pack(side="left", fill="both", expand=True)

        self.lbl_down = tk.Label(
            lines, text="\u2193 --.-", font=self.f_num, bg=self.bg, fg=self.fg,
            anchor="w", padx=0, pady=0,
        )
        self.lbl_down.pack(anchor="w")
        self.lbl_up = tk.Label(
            lines, text="\u2191 --.-", font=self.f_num, bg=self.bg, fg=self.fg,
            anchor="w", padx=0, pady=0,
        )
        self.lbl_up.pack(anchor="w")

        self.lbl_unit = tk.Label(
            wrap, text="Mb/s\n", font=self.f_unit, bg=self.bg, fg=self.muted,
            justify="center", padx=max(3, int(4 * self.scale)),
        )
        self.lbl_unit.pack(side="left", fill="y")

        self.root.update_idletasks()
        self.size = (
            self.root.winfo_reqwidth() + 2 * int(2 * self.scale),
            self.root.winfo_reqheight(),
        )

        self._bind_events()

        self.prev = None            # (index, in_octets, out_octets, monotonic)
        self.total_in = 0
        self.total_out = 0
        self.session_start = time.time()
        self.peak_down = 0.0
        self.peak_up = 0.0
        self.last_row = None        # last uplink row seen (for the tooltip)
        self.tick_count = 0
        self.tooltip = None
        self.tip_job = None

        self.place()

    # ------------------------------------------------------------------ placement
    def taskbar_rect(self):
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return None, None
        tb = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(tb))
        tray = user32.FindWindowExW(hwnd, None, "TrayNotifyWnd", None)
        tray_rect = None
        if tray:
            tr = wintypes.RECT()
            user32.GetWindowRect(tray, ctypes.byref(tr))
            tray_rect = tr
        return tb, tray_rect

    def place(self) -> None:
        tb, tray = self.taskbar_rect()
        if tb is None:
            return
        screen_h = user32.GetSystemMetrics(1)
        # Auto-hidden taskbar sits off-screen: get out of the way entirely.
        if tb.top >= screen_h - 2 or (tb.bottom - tb.top) < 10:
            self.root.withdraw()
            return
        self.root.deiconify()

        w, h = self.size
        right = (tray.left - int(6 * self.scale)) if tray else (tb.right - int(320 * self.scale))
        x = right - w
        y = tb.top + max(0, ((tb.bottom - tb.top) - h) // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ------------------------------------------------------------------ measuring
    def measure(self) -> None:
        rows = read_interface_rows()
        row = pick_uplink(rows)
        now = time.monotonic()

        if row is None:
            self.prev = None
            self.last_row = None
            self.set_lines("--.-", "--.-")
            self.status_bar.config(bg=self.col_off)
            self.root.title(f"{APP_NAME}: offline")
            return

        self.last_row = row
        if self.prev and self.prev[0] == row["index"]:
            dt = max(now - self.prev[3], 0.001)
            d_in = wrapped_delta(row["in_octets"], self.prev[1])
            d_out = wrapped_delta(row["out_octets"], self.prev[2])
            self.total_in += d_in
            self.total_out += d_out
            down_mbps = d_in * 8 / 1e6 / dt
            up_mbps = d_out * 8 / 1e6 / dt
        else:
            down_mbps = up_mbps = 0.0

        self.prev = (row["index"], row["in_octets"], row["out_octets"], now)
        self.peak_down = max(self.peak_down, down_mbps)
        self.peak_up = max(self.peak_up, up_mbps)

        self.set_lines(f"{down_mbps:.1f}", f"{up_mbps:.1f}")
        idle = down_mbps < 0.05 and up_mbps < 0.05
        self.status_bar.config(bg=self.col_idle if idle else self.col_active)
        self.root.title(
            f"{APP_NAME}: {row['descr']} - down {down_mbps:.1f} Mb/s, up {up_mbps:.1f} Mb/s"
        )
        if self.tooltip and self.tooltip.winfo_exists() and self.tooltip.state() == "normal":
            self.refresh_tooltip_text()

    def set_lines(self, down: str, up: str) -> None:
        self.lbl_down.config(text=f"\u2193 {down}")
        self.lbl_up.config(text=f"\u2191 {up}")

    # ------------------------------------------------------------------ tooltip
    def details(self) -> str:
        row = self.last_row
        if row is None:
            return "No active network adapter"
        uptime = int(time.time() - self.session_start)
        hours, rem = divmod(uptime, 3600)
        minutes, seconds = divmod(rem, 60)
        link = row["speed_bps"] / 1e6 if row["speed_bps"] else 0
        kind = IF_TYPE_NAMES.get(row["type"], f"type {row['type']}")
        return (
            f"Adapter: {row['descr']}\n"
            f"Link: {link:.0f} Mb/s   Media: {kind}\n"
            f"Down now: {self.lbl_down.cget('text')[2:]} Mb/s   Peak: {self.peak_down:.1f}\n"
            f"Up now: {self.lbl_up.cget('text')[2:]} Mb/s   Peak: {self.peak_up:.1f}\n"
            f"Session: {self.total_in / 1e6:.1f} MB down, {self.total_out / 1e6:.1f} MB up\n"
            f"Running for {hours}h {minutes:02d}m {seconds:02d}s"
        )

    def refresh_tooltip_text(self) -> None:
        if not self.tooltip:
            return
        for child in self.tooltip.winfo_children():
            child.destroy()
        tk.Label(
            self.tooltip, text=self.details(), justify="left", font=("Segoe UI", -int(12 * self.scale)),
            bg=self.bg, fg=self.fg, padx=8, pady=6, anchor="w",
        ).pack()

    def show_tooltip(self) -> None:
        self.hide_tooltip()
        self.tooltip = tk.Toplevel(self.root)
        self.tooltip.overrideredirect(True)
        self.tooltip.attributes("-topmost", True)
        self.tooltip.config(bg=self.bg)
        self.refresh_tooltip_text()
        self.tooltip.update_idletasks()
        w = self.tooltip.winfo_reqwidth()
        h = self.tooltip.winfo_reqheight()
        x = self.root.winfo_x() - w - 4
        y = self.root.winfo_y() - h - 4
        self.tooltip.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    def hide_tooltip(self) -> None:
        if self.tooltip and self.tooltip.winfo_exists():
            self.tooltip.destroy()
        self.tooltip = None

    def schedule_tooltip(self, _event=None) -> None:
        self.cancel_tooltip()
        self.tip_job = self.root.after(400, self.show_tooltip)

    def cancel_tooltip(self, _event=None) -> None:
        if self.tip_job:
            self.root.after_cancel(self.tip_job)
            self.tip_job = None
        self.hide_tooltip()

    # ------------------------------------------------------------------ events
    def _bind_events(self) -> None:
        for widget in (self.root, self.lbl_down, self.lbl_up, self.lbl_unit, self.status_bar):
            widget.bind("<Enter>", self.schedule_tooltip)
            widget.bind("<Leave>", self.cancel_tooltip)
            widget.bind("<Button-3>", self.open_menu)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Reset counters", command=self.reset_counters)
        self.menu.add_command(label="Reposition", command=self.place)
        self.menu.add_separator()
        self.menu.add_command(label="Exit", command=self.root.destroy)

    def open_menu(self, event) -> None:
        self.cancel_tooltip()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def reset_counters(self) -> None:
        self.total_in = self.total_out = 0
        self.peak_down = self.peak_up = 0.0
        self.session_start = time.time()
        self.prev = None

    # ------------------------------------------------------------------ loop
    def tick(self) -> None:
        self.tick_count += 1
        try:
            self.measure()
        except Exception:
            self.set_lines("--.-", "--.-")
            self.status_bar.config(bg=self.col_off)
        if self.tick_count % self.REPOSITION_EVERY == 0:
            try:
                self.place()
            except Exception:
                pass
        self.root.after(self.POLL_MS, self.tick)

    def run(self) -> None:
        self.tick()
        self.root.mainloop()


def main() -> int:
    if not single_instance_guard():
        return 0
    # System-aware DPI: keeps GetWindowRect pixel values consistent with tk geometry.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    NetworkIndicator().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
