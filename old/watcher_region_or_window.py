#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Watcher (Windows): Region OR Window Capture (OBS-style)
- Detects "done" when pixels are stable AND (optional) OCR keyword appears.

Deps:
  - Windows
  - Tesseract OCR on PATH
  - pip install mss pillow pytesseract numpy pywin32
"""

import time
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

import pytesseract

import tkinter as tk
from tkinter import ttk, messagebox

import platform, shutil, subprocess, sys

# Region capture
from mss import mss

# Window capture (HWND)
import win32gui, win32ui, win32con

DONE_WAV = "done.wav"

@dataclass
class WatchConfig:
    poll_secs: float = 0.8
    stable_secs: int = 6
    pixel_threshold: float = 0.005  # MAD threshold on 64x64 gray
    require_keyword: bool = True
    require_end_punct: bool = False
    keywords: List[str] = None
    progress_words: List[str] = None

    def __post_init__(self):
        if self.keywords is None:
            self.keywords = ["You", "you"]
        if self.progress_words is None:
            self.progress_words = ["typing", "loading", "generating", "thinking"]

def normalize_text(s: str) -> str:
    return " ".join(s.split())

def text_contains_any(hay: str, needles: List[str]) -> bool:
    H = hay.lower()
    return any((n or "").lower() in H for n in needles)

def ends_like_finished(text: str) -> bool:
    text = text.strip()
    if not text: return False
    return text[-1] in [".","!","?","»","”","'","\"",")","]","…"]

def capture_region(mss_obj: mss, region: Tuple[int,int,int,int]) -> Image.Image:
    left, top, w, h = region
    shot = mss_obj.grab({"left": left, "top": top, "width": w, "height": h})
    return Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)

def list_windows() -> List[str]:
    titles = []
    def _enum_cb(hwnd, extra):
        title = win32gui.GetWindowText(hwnd)
        if title and win32gui.IsWindowVisible(hwnd):
            titles.append(title)
    win32gui.EnumWindows(_enum_cb, None)
    # Dedup preserve order
    out, seen = [], set()
    for t in titles:
        if t not in seen:
            out.append(t); seen.add(t)
    return out

def find_hwnd_by_title_fragment(fragment: str) -> Optional[int]:
    fragment = (fragment or "").lower().strip()
    if not fragment: return None
    found = []
    def _enum_cb(hwnd, extra):
        title = (win32gui.GetWindowText(hwnd) or "").lower()
        if fragment in title and win32gui.IsWindowVisible(hwnd):
            found.append(hwnd)
    win32gui.EnumWindows(_enum_cb, None)
    return found[0] if found else None

def capture_window(hwnd: int) -> Optional[Image.Image]:
    """Try to capture a window even if covered (PrintWindow)."""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w, h = max(1, right-left), max(1, bottom-top)
        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        saveBM = win32ui.CreateBitmap()
        saveBM.CreateCompatibleBitmap(mfcDC, w, h)
        saveDC.SelectObject(saveBM)
        ok = win32gui.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)
        if not ok:
            win32gui.PrintWindow(hwnd, saveDC.GetSafeHdc(), 0)
        bmpinfo = saveBM.GetInfo()
        bmpstr  = saveBM.GetBitmapBits(True)
        img = Image.frombuffer('RGB', (bmpinfo['bmWidth'], bmpinfo['bmHeight']), bmpstr, 'raw', 'BGRX', 0, 1)
        win32gui.ReleaseDC(hwnd, hwndDC)
        saveDC.DeleteDC(); mfcDC.DeleteDC(); win32gui.DeleteObject(saveBM.GetHandle())
        return img
    except Exception:
        try:
            win32gui.ReleaseDC(hwnd, hwndDC)
        except Exception:
            pass
        return None

class RegionSelector:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.overlay = None
        self.canvas = None
        self.start = None
        self.rect = None
        self.result = None

    def select(self) -> Optional[Tuple[int,int,int,int]]:
        if self.overlay is not None: return None
        self.overlay = tk.Toplevel(self.root)
        self.overlay.attributes("-fullscreen", True)
        try: self.overlay.attributes("-alpha", 0.3)
        except tk.TclError: pass
        self.overlay.configure(bg="black")
        self.overlay.attributes("-topmost", True)
        self.canvas = tk.Canvas(self.overlay, bg="black", highlightthickness=0, cursor="cross")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.overlay.grab_set()
        self.root.wait_window(self.overlay)
        return self.result

    def _on_press(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect); self.rect=None

    def _on_drag(self, e):
        if not self.start: return
        x0,y0 = self.start; x1,y1 = e.x,e.y
        if self.rect:
            self.canvas.coords(self.rect, x0,y0,x1,y1)
        else:
            self.rect = self.canvas.create_rectangle(x0,y0,x1,y1, outline="red", width=2)

    def _on_release(self, e):
        if not self.start: return
        x0,y0 = self.start; x1,y1 = e.x,e.y
        self.overlay.destroy(); self.overlay=None; self.canvas=None; self.rect=None; self.start=None
        left, top = min(x0,x1), min(y0,y1)
        w, h = abs(x1-x0), abs(y1-y0)
        if w<10 or h<10:
            messagebox.showerror("Region too small", "Drag a larger region."); self.result=None; return
        self.result = (left, top, w, h)

class WatcherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Watcher — Region or Window (OBS-style)")
        self.cfg = WatchConfig()
        self.running = False
        self.thread = None
        self.last_vec = None
        self.last_change = time.time()
        self.text_snapshot = ""

        self.mode_var = tk.StringVar(value="region")  # "region" or "window"
        self.region: Optional[Tuple[int,int,int,int]] = None
        self.sct = mss()

        self.hwnd: Optional[int] = None
        self.win_list_var = tk.StringVar(value=[])

        # UI
        frm = ttk.Frame(root, padding=12); frm.grid(sticky="nsew")
        root.columnconfigure(0, weight=1); root.rowconfigure(0, weight=1)

        # Mode
        ttk.Label(frm, text="Capture Mode:").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(frm, text="Region", variable=self.mode_var, value="region").grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(frm, text="Window", variable=self.mode_var, value="window").grid(row=0, column=2, sticky="w")

        # Region controls
        ttk.Button(frm, text="Select Region", command=self.select_region).grid(row=1, column=0, sticky="ew", pady=(6,0))
        self.region_lbl = ttk.Label(frm, text="Region: (none)")
        self.region_lbl.grid(row=1, column=1, columnspan=2, sticky="w", pady=(6,0))

        # Window controls
        self.listbox = tk.Listbox(frm, listvariable=self.win_list_var, height=8)
        self.listbox.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(6,0))
        ttk.Button(frm, text="Refresh Windows", command=self.refresh_windows).grid(row=2, column=2, sticky="ew", pady=(6,0))
        ttk.Label(frm, text="Filter:").grid(row=3, column=0, sticky="w")
        self.filter_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.filter_var).grid(row=3, column=1, sticky="ew")
        ttk.Button(frm, text="Find First", command=self.find_first).grid(row=3, column=2, sticky="ew")

        # Settings
        ttk.Label(frm, text="Stable seconds:").grid(row=4, column=0, sticky="w", pady=(10,0))
        self.stable_var = tk.IntVar(value=self.cfg.stable_secs)
        ttk.Entry(frm, textvariable=self.stable_var, width=8).grid(row=4, column=1, sticky="w", pady=(10,0))

        ttk.Label(frm, text="Poll interval (s):").grid(row=4, column=2, sticky="w", pady=(10,0))
        self.poll_var = tk.DoubleVar(value=self.cfg.poll_secs)
        ttk.Entry(frm, textvariable=self.poll_var, width=8).grid(row=4, column=3, sticky="w", pady=(10,0))

        ttk.Label(frm, text="Keywords (comma-separated):").grid(row=5, column=0, columnspan=4, sticky="w", pady=(6,0))
        self.kw_var = tk.StringVar(value=", ".join(self.cfg.keywords))
        ttk.Entry(frm, textvariable=self.kw_var).grid(row=6, column=0, columnspan=4, sticky="ew")

        self.require_kw_var = tk.BooleanVar(value=self.cfg.require_keyword)
        ttk.Checkbutton(frm, text="Require keyword", variable=self.require_kw_var).grid(row=7, column=0, sticky="w", pady=(6,0))

        self.require_punct_var = tk.BooleanVar(value=self.cfg.require_end_punct)
        ttk.Checkbutton(frm, text="Punct end required", variable=self.require_punct_var).grid(row=7, column=1, sticky="w", pady=(6,0))

        ttk.Label(frm, text="Avoid words (comma-separated):").grid(row=8, column=0, columnspan=4, sticky="w", pady=(6,0))
        self.avoid_var = tk.StringVar(value=", ".join(self.cfg.progress_words))
        ttk.Entry(frm, textvariable=self.avoid_var).grid(row=9, column=0, columnspan=4, sticky="ew")

        # Start/Stop + status
        self.btn_start = ttk.Button(frm, text="Start", command=self.start, state="normal")
        self.btn_start.grid(row=10, column=2, sticky="ew", pady=(10,0))
        self.btn_stop = ttk.Button(frm, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.grid(row=10, column=3, sticky="ew", pady=(10,0))

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(frm, textvariable=self.status_var).grid(row=11, column=0, columnspan=4, sticky="w", pady=(10,0))

        # layout
        for c in range(4):
            frm.columnconfigure(c, weight=1)
        frm.rowconfigure(2, weight=1)

        self.refresh_windows()

    def select_region(self):
        if self.mode_var.get() != "region":
            messagebox.showinfo("Mode", "Switch to 'Region' mode to select.")
            return
        sel = RegionSelector(self.root)
        r = sel.select()
        if r:
            self.region = r
            self.region_lbl.config(text=f"Region: left={r[0]} top={r[1]} w={r[2]} h={r[3]}")

    def refresh_windows(self):
        titles = list_windows()
        self.win_list_var.set(titles)
        self.status_var.set(f"Found {len(titles)} windows.")

    def find_first(self):
        frag = self.filter_var.get().strip()
        if not frag:
            messagebox.showinfo("Filter empty", "Enter a title fragment first (e.g., 'Chrome' or 'ChatGPT').")
            return
        hwnd = find_hwnd_by_title_fragment(frag)
        if not hwnd:
            messagebox.showwarning("Not found", f"No visible window contains: {frag}")
            return
        title = win32gui.GetWindowText(hwnd)
        try:
            idx = list(self.win_list_var.get()).index(title)
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(idx); self.listbox.see(idx)
            self.status_var.set(f"Selected: {title}")
        except ValueError:
            self.status_var.set(f"Found: {title}")

    def start(self):
        # Sync config
        self.cfg.stable_secs = int(self.stable_var.get())
        self.cfg.poll_secs = float(self.poll_var.get())
        self.cfg.require_keyword = bool(self.require_kw_var.get())
        self.cfg.require_end_punct = bool(self.require_punct_var.get())
        self.cfg.keywords = [k.strip() for k in self.kw_var.get().split(",") if k.strip()]
        self.cfg.progress_words = [k.strip() for k in self.avoid_var.get().split(",") if k.strip()]

        self.last_vec = None
        self.last_change = time.time()
        self.text_snapshot = ""
        mode = self.mode_var.get()

        if mode == "region":
            if not self.region:
                messagebox.showerror("No region", "Select a region first.")
                return
        else:
            sel = self.listbox.curselection()
            if not sel and self.filter_var.get().strip():
                self.find_first(); sel = self.listbox.curselection()
            if not sel:
                messagebox.showerror("No window", "Select a window or use the filter.")
                return
            title = self.listbox.get(sel[0])
            self.hwnd = find_hwnd_by_title_fragment(title)
            if not self.hwnd:
                messagebox.showerror("Window missing", "Window disappeared. Refresh and try again.")
                return

        self.running = True
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        self.status_var.set(f"Watching ({mode})...")

    def stop(self):
        self.running = False
        self.btn_stop.config(state="disabled")
        self.btn_start.config(state="normal")
        self.status_var.set("Stopped.")

    def loop(self):
        while self.running:
            mode = self.mode_var.get()
            if mode == "region":
                img = capture_region(mss(), self.region)
            else:
                img = capture_window(self.hwnd)
                if img is None:
                    self.status_var.set("Capture failed (PrintWindow). Try keeping window visible or disable GPU accel.")
                    time.sleep(self.cfg.poll_secs); continue

            # Pixel stability
            g = img.convert("L").resize((64,64))
            arr = np.asarray(g, dtype=np.float32)/255.0
            vec = arr.flatten()
            changed = False
            if self.last_vec is None:
                changed = True
            else:
                mad = float(np.mean(np.abs(vec - self.last_vec)))
                changed = mad > self.cfg.pixel_threshold
            self.last_vec = vec
            if changed:
                self.last_change = time.time()

            # OCR snapshot
            arru = np.asarray(img.convert("L"), dtype=np.uint8)
            thr = (arru > 200).astype(np.uint8) * 255
            txt = pytesseract.image_to_string(Image.fromarray(thr))
            clean = normalize_text(txt)
            if clean != self.text_snapshot:
                self.text_snapshot = clean
                self.last_change = time.time()

            # Decide
            now = time.time()
            stable = now - self.last_change
            have_kw = True
            if self.cfg.require_keyword:
                have_kw = text_contains_any(self.text_snapshot, self.cfg.keywords)
            if self.cfg.require_end_punct and not ends_like_finished(self.text_snapshot):
                have_kw = False
            avoid = text_contains_any(self.text_snapshot, self.cfg.progress_words)

            self.status_var.set(f"Stable: {stable:.1f}s | Keyword: {have_kw} | Avoid: {avoid}")
            if stable >= self.cfg.stable_secs and have_kw and not avoid:
                self._ding()
                self.stop()
                break

            time.sleep(self.cfg.poll_secs)

    def _ding(self):
        try:
            import winsound
            winsound.PlaySound(DONE_WAV, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        except Exception:
            pass
        try:
            import winsound as _ws
            _ws.Beep(1000, 400); return
        except Exception:
            pass
        sys.stdout.write("\a"); sys.stdout.flush()

def main():
    if platform.system() != "Windows":
        print("This build targets Windows.")
        return
    root = tk.Tk()
    app = WatcherApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
