"""
Apple TV Screen Mirror for Windows (prototype)

How it works:
  1. ffmpeg captures the desktop and writes a live HLS stream to a temp folder.
  2. A tiny HTTP server shares that folder on your local network.
  3. pyatv tells the Apple TV (over AirPlay) to play the stream URL.

Expect a few seconds of delay. Not suitable for gaming.

Requirements:  pip install pyatv
               ffmpeg.exe on PATH  (https://www.gyan.dev/ffmpeg/builds/)
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from tkinter import messagebox, simpledialog, ttk

import pyatv
from pyatv.const import Protocol

CRED_FILE = os.path.join(os.path.expanduser("~"), ".appletv_mirror.json")
HTTP_PORT = 8765


def find_ffmpeg():
    """Look for ffmpeg.exe next to the app (or bundled), then on PATH."""
    base = getattr(sys, "_MEIPASS", None)
    folders = [base] if base else []
    folders.append(os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)))
    for d in folders:
        p = os.path.join(d, "ffmpeg.exe")
        if os.path.exists(p):
            return p
    return shutil.which("ffmpeg")


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def load_creds():
    try:
        with open(CRED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_creds(c):
    with open(CRED_FILE, "w") as f:
        json.dump(c, f)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


class App:
    def __init__(self, root):
        self.root = root
        root.title("Apple TV Mirror")
        root.geometry("420x360")
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        self.devices = []
        self.ffmpeg = None
        self.httpd = None
        self.tmpdir = None
        self.atv = None
        self.play_future = None

        ttk.Label(root, text="Apple TVs on your network:").pack(anchor="w", padx=10, pady=(10, 0))
        self.listbox = tk.Listbox(root, height=6, exportselection=False)
        self.listbox.pack(fill="x", padx=10, pady=5)

        row = ttk.Frame(root)
        row.pack(fill="x", padx=10)
        ttk.Button(row, text="Scan", command=self.scan).pack(side="left")
        self.start_btn = ttk.Button(row, text="Start mirroring", command=self.start)
        self.start_btn.pack(side="left", padx=5)
        self.stop_btn = ttk.Button(row, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")

        opt = ttk.Frame(root)
        opt.pack(fill="x", padx=10, pady=10)
        ttk.Label(opt, text="Quality:").pack(side="left")
        self.quality = ttk.Combobox(opt, values=["720p", "1080p"], state="readonly", width=8)
        self.quality.set("1080p")
        self.quality.pack(side="left", padx=5)
        self.audio = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Capture audio (needs virtual-audio-capturer)",
                        variable=self.audio).pack(side="left", padx=5)

        self.status = tk.StringVar(value="Click Scan to find Apple TVs.")
        ttk.Label(root, textvariable=self.status, wraplength=390).pack(anchor="w", padx=10, pady=5)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- helpers ----------
    def set_status(self, text):
        self.root.after(0, lambda: self.status.set(text))

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def ask_pin(self, name):
        result, done = {}, threading.Event()

        def ask():
            result["pin"] = simpledialog.askstring(
                "Pairing", f"Enter the PIN shown on {name}:", parent=self.root)
            done.set()

        self.root.after(0, ask)
        done.wait()
        return result["pin"]

    # ---------- scan ----------
    def scan(self):
        self.set_status("Scanning...")
        self.run(self._scan())

    async def _scan(self):
        found = await pyatv.scan(self.loop, timeout=5)
        self.devices = [d for d in found if d.get_service(Protocol.AirPlay)]

        def update():
            self.listbox.delete(0, "end")
            for d in self.devices:
                self.listbox.insert("end", f"{d.name}  ({d.address})")
            self.status.set(f"Found {len(self.devices)} device(s).")

        self.root.after(0, update)

    # ---------- pairing ----------
    async def ensure_paired(self, conf):
        creds = load_creds()
        key = conf.identifier
        if key in creds:
            conf.set_credentials(Protocol.AirPlay, creds[key])
            return True
        self.set_status("Pairing: look for a PIN on your TV...")
        pairing = await pyatv.pair(conf, Protocol.AirPlay, self.loop)
        await pairing.begin()
        if pairing.device_provides_pin:
            pin = await self.loop.run_in_executor(None, self.ask_pin, conf.name)
            if not pin:
                await pairing.close()
                return False
            pairing.pin(pin)
        await pairing.finish()
        ok = pairing.has_paired
        if ok:
            creds[key] = pairing.service.credentials
            save_creds(creds)
            conf.set_credentials(Protocol.AirPlay, pairing.service.credentials)
        await pairing.close()
        return ok

    # ---------- start / stop ----------
    def start(self):
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("Select a device", "Scan and select an Apple TV first.")
            return
        if not find_ffmpeg():
            messagebox.showerror("ffmpeg missing", "Put ffmpeg.exe next to this app.")
            return
        conf = self.devices[sel[0]]
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.play_future = self.run(self._start(conf))

    async def _start(self, conf):
        try:
            if not await self.ensure_paired(conf):
                self.set_status("Pairing failed or cancelled.")
                return self.reset_buttons()

            self.tmpdir = tempfile.mkdtemp(prefix="atvmirror_")
            width = "1920" if self.quality.get() == "1080p" else "1280"
            bitrate = "6M" if self.quality.get() == "1080p" else "3M"
            cmd = [find_ffmpeg(), "-loglevel", "error", "-f", "gdigrab", "-framerate", "30",
                   "-i", "desktop"]
            if self.audio.get():
                cmd += ["-f", "dshow", "-i", "audio=virtual-audio-capturer"]
            cmd += ["-vf", f"scale={width}:-2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-g", "30",
                    "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]
            if self.audio.get():
                cmd += ["-c:a", "aac", "-b:a", "128k"]
            cmd += ["-f", "hls", "-hls_time", "1", "-hls_list_size", "5",
                    "-hls_flags", "delete_segments+independent_segments",
                    os.path.join(self.tmpdir, "stream.m3u8")]
            self.ffmpeg = subprocess.Popen(
                cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

            handler = partial(QuietHandler, directory=self.tmpdir)
            self.httpd = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), handler)
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

            self.set_status("Starting capture...")
            playlist = os.path.join(self.tmpdir, "stream.m3u8")
            for _ in range(60):
                if os.path.exists(playlist) and len(os.listdir(self.tmpdir)) > 2:
                    break
                await asyncio.sleep(0.5)

            url = f"http://{local_ip()}:{HTTP_PORT}/stream.m3u8"
            self.atv = await pyatv.connect(conf, self.loop)
            self.set_status(f"Mirroring to {conf.name}. (Windows Firewall may ask for "
                            f"permission on port {HTTP_PORT} - allow it.)")
            await self.atv.stream.play_url(url)
        except Exception as e:
            self.set_status(f"Error: {e}")
        finally:
            self.cleanup()
            self.reset_buttons()

    def stop(self):
        self.cleanup()
        self.reset_buttons()
        self.set_status("Stopped.")

    def cleanup(self):
        if self.atv:
            try:
                self.atv.close()
            except Exception:
                pass
            self.atv = None
        if self.ffmpeg:
            self.ffmpeg.terminate()
            self.ffmpeg = None
        if self.httpd:
            self.httpd.shutdown()
            self.httpd = None
        if self.tmpdir:
            time.sleep(0.3)
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None

    def reset_buttons(self):
        self.root.after(0, lambda: (self.start_btn.config(state="normal"),
                                    self.stop_btn.config(state="disabled")))

    def on_close(self):
        self.cleanup()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
