# OCR + Pixel Watcher (Chat-style "Done" Detector)

This tiny desktop app watches a region of your screen and **plays a sound** when BOTH of these are true:
1) The region's pixels have been **stable** (no meaningful changes) for N seconds, and
2) An **OCR keyword** (e.g., your username like "You" or "David") has reappeared in the region (typical of chat UIs when they're ready for input).

No cloud, no API fees — all local.

## Install

1. Install **Tesseract OCR**:
   - **macOS**: `brew install tesseract`
   - **Windows**: `choco install tesseract` or download from tesseract-ocr.github.io
   - **Ubuntu/Debian**: `sudo apt-get install tesseract-ocr`

2. Install Python deps (Python 3.9+):
   ```bash
   pip install -r requirements.txt
   ```

## Run

```bash
python ocr_pixel_watcher.py
```

- Click **Select Region**, then drag a rectangle over the chat output area (or any log/output region).
- Set your **Username/keyword** (comma-separated allowed, e.g., `You, David`).
- Adjust **Stable seconds** and **Poll interval** if needed.
- Click **Start**. It will **ding** when the output stops changing and the username keyword is visible by OCR.
- Click **Stop** to end monitoring.

### Tips
- If you only want "stable" detection (no keyword), uncheck **"Require keyword"**.
- If OCR is noisy, enable **"Punct end required"** so text must end with punctuation before we count stability.
- Narrow the watch region to reduce OCR load and false positives.
- You can add common "still working" words (e.g., `generating, typing, loading`) to avoid early triggers.

## Notes
- On some Windows/macOS setups, if the sound doesn't play, it will fallback to a terminal bell.
- You can replace `done.wav` with any short WAV file of your choice.
