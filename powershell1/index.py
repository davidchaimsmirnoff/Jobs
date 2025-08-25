# index.py — PowerShell ConPTY wrapper with GUI + per-sound volume
# - Continuous short tick while output flows
# - Low stop beep after QUIET_SEC of silence
# - GUI: Mute, Master Volume, Running Tick Volume, Stop Beep Volume, all other tuning
from winpty import PtyProcess
import threading, time, sys, msvcrt, signal, struct, math, winsound, tempfile, os

# ===================== Shared state (GUI <-> workers) =====================
mute = False
master_volume_pct = 60   # 0..100
run_volume_pct    = 50   # 0..100  (ticks)
stop_volume_pct   = 70   # 0..100  (stop tones)
state_lock = threading.Lock()

# --- defaults (tweak in GUI) ---
QUIET_SEC = 3.0             # silence window before stop beep
RUN_BEEP_FREQ = 600         # Hz while bytes are flowing
RUN_BEEP_MS   = 30          # ms each running tick
RUN_BEEP_GAP  = 120         # ms gap between ticks
STOP_BEEP_1   = (440, 160)  # (Hz, ms) first stop tone
STOP_BEEP_2   = (330, 160)  # second stop tone (set to None to disable)

# ===================== Tone synth with volume (no extra deps) =====================
def _wav_bytes(freq_hz: int, dur_ms: int, vol_pct: int, sample_rate=44100) -> bytes:
    n = max(1, int(sample_rate * (dur_ms / 1000.0)))
    vol = max(0, min(vol_pct, 100)) / 100.0
    amp = int(32767 * vol)
    frames = bytearray()
    w = 2.0 * math.pi * float(freq_hz)
    for i in range(n):
        s = int(amp * math.sin(w * (i / sample_rate)))
        frames += struct.pack("<h", s)
    data = bytes(frames)
    sub2 = len(data)
    chunk = 36 + sub2
    byte_rate = sample_rate * 2  # 16-bit mono
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", chunk, b"WAVE",
        b"fmt ", 16, 1, 1, sample_rate, byte_rate, 2, 16,
        b"data", sub2
    )
    return hdr + data

def _effective_volume(per_sound_pct: int) -> int:
    """Combine master + per-sound volume, clamp 0..100."""
    with state_lock:
        if mute: return 0
        m = master_volume_pct
    eff = int(max(0, min(100, (m * per_sound_pct) / 100)))
    return eff

def _play_wav_async(wav: bytes, dur_ms: int):
    """Play a WAV safely in a background thread."""
    def _worker():
        try:
            winsound.PlaySound(wav, winsound.SND_MEMORY)  # sync in this thread
        except Exception:
            # Fallback: temp file + async
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
                    f.write(wav)
                    path = f.name
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                time.sleep(dur_ms / 1000.0 + 0.05)
                try: os.remove(path)
                except OSError: pass
            except Exception:
                winsound.MessageBeep(-1)
    threading.Thread(target=_worker, daemon=True).start()

def play_tick(freq, dur_ms):
    """Running tick (uses run_volume_pct)."""
    vol = _effective_volume(run_volume_pct)
    if vol <= 0 or dur_ms <= 0 or freq <= 0: return
    _play_wav_async(_wav_bytes(int(freq), int(dur_ms), vol), int(dur_ms))

def play_stop_beeps():
    """Stop tones (use stop_volume_pct)."""
    vol = _effective_volume(stop_volume_pct)
    if vol <= 0: return
    with state_lock:
        sb1 = STOP_BEEP_1
        sb2 = STOP_BEEP_2
    if sb1:
        _play_wav_async(_wav_bytes(int(sb1[0]), int(sb1[1]), vol), int(sb1[1]))
        if sb2: time.sleep(0.05)
    if sb2:
        _play_wav_async(_wav_bytes(int(sb2[0]), int(sb2[1]), vol), int(sb2[1]))

# ===================== ConPTY wrapper (PowerShell inside) =====================
proc = PtyProcess.spawn("powershell.exe")
writing = False
last_out = time.time()
alive = True

def reader():
    """Mirror child output; mark 'writing' when bytes arrive."""
    global writing, last_out, alive
    try:
        while proc.isalive():
            chunk = proc.read(4096)  # str
            if not chunk:
                break
            sys.stdout.write(chunk)
            sys.stdout.flush()
            writing = True
            last_out = time.time()
    finally:
        alive = False

def writer():
    """Forward keystrokes to the child shell."""
    while alive and proc.isalive():
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch == '\x03':  # Ctrl+C
                proc.write('\x03'); continue
            if ch == '\r':   # Enter -> CRLF
                proc.write('\r\n'); continue
            proc.write(ch)
        else:
            time.sleep(0.01)

def idle_watcher():
    """If quiet for QUIET_SEC, play stop beep and drop to not-writing."""
    global writing
    while alive:
        with state_lock: q = QUIET_SEC
        if writing and (time.time() - last_out) >= q:
            writing = False
            play_stop_beeps()
        time.sleep(0.05)

def running_beeper():
    """Emit periodic ticks while 'writing' is True."""
    while alive:
        if writing:
            with state_lock:
                f = RUN_BEEP_FREQ
                ms = RUN_BEEP_MS
                gap = RUN_BEEP_GAP
            play_tick(f, ms)
            time.sleep(max(0.0, gap / 1000.0))
        else:
            time.sleep(0.05)

def sigint(_sig, _frm):
    try:
        proc.write('\x03')
    except Exception:
        pass
signal.signal(signal.SIGINT, sigint)

# ===================== GUI =====================
def start_gui():
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Beep Controls")

    def grid(w, r, c, **kw): w.grid(row=r, column=c, padx=6, pady=4, **kw)
    main = ttk.Frame(root, padding=10); main.pack(fill="both", expand=True)

    # Mute + Master Volume
    mute_var = tk.BooleanVar(value=False)
    def on_mute():
        global mute
        with state_lock: mute = bool(mute_var.get())
    ttk.Checkbutton(main, text="Mute", variable=mute_var, command=on_mute).grid(row=0, column=0, sticky="w")

    ttk.Label(main, text="Master Volume:").grid(row=0, column=1, sticky="e")
    mvol_var = tk.IntVar(value=master_volume_pct)
    def on_mvol(_=None):
        global master_volume_pct
        with state_lock: master_volume_pct = int(mvol_var.get())
    mvol = ttk.Scale(main, from_=0, to=100, orient="horizontal", variable=mvol_var, command=lambda _v: on_mvol())
    grid(mvol, 0, 2, sticky="ew"); main.grid_columnconfigure(2, weight=1)

    # Running tick controls
    ttk.Separator(main, orient="horizontal").grid(row=1, column=0, columnspan=4, sticky="ew", pady=4)
    ttk.Label(main, text="Running tick (while output flows)").grid(row=2, column=0, columnspan=4, sticky="w")

    ttk.Label(main, text="Tick Volume:").grid(row=3, column=0, sticky="e")
    rvol_var = tk.IntVar(value=run_volume_pct)
    def on_rvol(_=None):
        global run_volume_pct
        with state_lock: run_volume_pct = int(rvol_var.get())
    rvol = ttk.Scale(main, from_=0, to=100, orient="horizontal", variable=rvol_var, command=lambda _v: on_rvol())
    grid(rvol, 3, 1, sticky="ew")

    ttk.Label(main, text="Freq (Hz):").grid(row=4, column=0, sticky="e")
    rf_var = tk.IntVar(value=RUN_BEEP_FREQ)
    rf_spin = ttk.Spinbox(main, from_=100, to=4000, increment=10, textvariable=rf_var, width=8); grid(rf_spin, 4, 1)

    ttk.Label(main, text="Dur (ms):").grid(row=5, column=0, sticky="e")
    rms_var = tk.IntVar(value=RUN_BEEP_MS)
    rms_spin = ttk.Spinbox(main, from_=5, to=500, increment=5, textvariable=rms_var, width=8); grid(rms_spin, 5, 1)

    ttk.Label(main, text="Gap (ms):").grid(row=6, column=0, sticky="e")
    rgap_var = tk.IntVar(value=RUN_BEEP_GAP)
    rgap_spin = ttk.Spinbox(main, from_=20, to=2000, increment=10, textvariable=rgap_var, width=8); grid(rgap_spin, 6, 1)

    def apply_run():
        global RUN_BEEP_FREQ, RUN_BEEP_MS, RUN_BEEP_GAP
        with state_lock:
            RUN_BEEP_FREQ = int(rf_var.get())
            RUN_BEEP_MS   = int(rms_var.get())
            RUN_BEEP_GAP  = int(rgap_var.get())
    ttk.Button(main, text="Apply", command=apply_run).grid(row=7, column=2, sticky="w")
    ttk.Button(main, text="Test Tick", command=lambda: play_tick(rf_var.get(), rms_var.get())).grid(row=7, column=0, sticky="w")

    # Stop beep controls
    ttk.Separator(main, orient="horizontal").grid(row=8, column=0, columnspan=4, sticky="ew", pady=4)
    ttk.Label(main, text="Stop beep(s) after silence").grid(row=9, column=0, columnspan=4, sticky="w")

    ttk.Label(main, text="Stop Volume:").grid(row=10, column=0, sticky="e")
    svol_var = tk.IntVar(value=stop_volume_pct)
    def on_svol(_=None):
        global stop_volume_pct
        with state_lock: stop_volume_pct = int(svol_var.get())
    svol = ttk.Scale(main, from_=0, to=100, orient="horizontal", variable=svol_var, command=lambda _v: on_svol())
    grid(svol, 10, 1, sticky="ew")

    ttk.Label(main, text="Stop #1 Freq:").grid(row=11, column=0, sticky="e")
    s1f_var = tk.IntVar(value=STOP_BEEP_1[0]); s1f_spin = ttk.Spinbox(main, from_=100, to=4000, increment=10, textvariable=s1f_var, width=8); grid(s1f_spin, 11, 1)
    ttk.Label(main, text="Stop #1 Dur:").grid(row=12, column=0, sticky="e")
    s1d_var = tk.IntVar(value=STOP_BEEP_1[1]); s1d_spin = ttk.Spinbox(main, from_=20, to=2000, increment=10, textvariable=s1d_var, width=8); grid(s1d_spin, 12, 1)

    enable_s2 = tk.BooleanVar(value=STOP_BEEP_2 is not None)
    ttk.Checkbutton(main, text="Enable Stop #2", variable=enable_s2).grid(row=13, column=0, sticky="w")
    ttk.Label(main, text="Stop #2 Freq:").grid(row=14, column=0, sticky="e")
    s2f_var = tk.IntVar(value=(STOP_BEEP_2[0] if STOP_BEEP_2 else 330)); s2f_spin = ttk.Spinbox(main, from_=100, to=4000, increment=10, textvariable=s2f_var, width=8); grid(s2f_spin, 14, 1)
    ttk.Label(main, text="Stop #2 Dur:").grid(row=15, column=0, sticky="e")
    s2d_var = tk.IntVar(value=(STOP_BEEP_2[1] if STOP_BEEP_2 else 160)); s2d_spin = ttk.Spinbox(main, from_=20, to=2000, increment=10, textvariable=s2d_var, width=8); grid(s2d_spin, 15, 1)

    ttk.Label(main, text="Silence before stop (sec):").grid(row=16, column=0, sticky="e")
    q_var = tk.DoubleVar(value=QUIET_SEC)
    q_spin = ttk.Spinbox(main, from_=0.2, to=10.0, increment=0.1, textvariable=q_var, width=8); grid(q_spin, 16, 1)

    def apply_stop():
        global STOP_BEEP_1, STOP_BEEP_2, QUIET_SEC
        with state_lock:
            STOP_BEEP_1 = (int(s1f_var.get()), int(s1d_var.get()))
            STOP_BEEP_2 = (int(s2f_var.get()), int(s2d_var.get())) if enable_s2.get() else None
            QUIET_SEC   = float(q_var.get())
    ttk.Button(main, text="Apply", command=apply_stop).grid(row=17, column=2, sticky="w")
    ttk.Button(main, text="Test Stop Beep", command=play_stop_beeps).grid(row=17, column=0, sticky="w")

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()

# ===================== Start worker threads =====================
threads = [
    threading.Thread(target=reader, daemon=True),
    threading.Thread(target=writer, daemon=True),
    threading.Thread(target=idle_watcher, daemon=True),
    threading.Thread(target=running_beeper, daemon=True),
]
for t in threads: t.start()

# ===================== Start GUI (non-blocking) =====================
gui_thread = threading.Thread(target=start_gui, daemon=True)
gui_thread.start()

# Wait for child to exit
threads[0].join()
