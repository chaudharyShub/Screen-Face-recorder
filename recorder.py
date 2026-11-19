"""Screen Face Recorder - minimal screen + webcam (bottom-left) recorder.

While recording, three light ffmpeg processes each write one source to its own file
in a folder per recording: screen.mkv (ddagrab GPU capture -> h264_amf), face.mkv
(webcam or phone), audio.mka (mic), plus sync-info.txt describing how to line them up.
Combining them is left to the user (e.g. with an AI tool).

A/V sync: every source is re-timestamped onto one clock ("seconds since Start was
pressed") using when it was actually captured, because each source natively uses a
different clock and ffmpeg would otherwise zero each one at its own start.
"""
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from phone_cam import PhoneCam

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
FPS = 30
QUALITY = {  # label: (width, height, AMF quality preset, qp)
    "1080p (sharp)": (1920, 1080, "balanced", 20),
    "720p (lighter)": (1280, 720, "speed", 23),
}
CAM_SIZES = {"Small": 0.22, "Medium": 0.30, "Large": 0.42}  # bubble diameter / video height
MASK_DIR = Path(os.environ.get("TEMP", ".")) / "screen_face_recorder"
PHONE = "Phone camera (Wi-Fi)"
# Phone frames reach the PC a little after they were filmed (camera + JPEG + Wi-Fi);
# shift them earlier by this much so lips match the voice.
PHONE_LATENCY = 0.12


def qpc_seconds():
    """Windows performance-counter clock - the clock dshow (webcam/mic) timestamps use."""
    c, f = ctypes.c_int64(), ctypes.c_int64()
    ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(c))
    ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(f))
    return c.value / f.value


def list_devices():
    """Return (video_devices, audio_devices) using ffmpeg's dshow listing."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    ).stderr
    video, audio = [], []
    for name, kind in re.findall(r'"([^"]+)" \((video|audio)\)', out):
        if "OBS Virtual" in name:
            continue
        (video if kind == "video" else audio).append(name)
    return video, audio


def has_amf():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return "h264_amf" in out


def encoder_args(amf, preset, qp):
    """AMD hardware encoding when available; fall back to fast x264."""
    if amf:
        return ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", preset,
                "-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(qp + 2), "-bf", "0"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", str(qp), "-tune", "zerolatency"]


def circle_mask(d):
    """Create (once) a soft-edged circular alpha mask PNG of diameter d."""
    MASK_DIR.mkdir(parents=True, exist_ok=True)
    path = MASK_DIR / f"circle_{d}.png"
    if not path.exists():
        r = d / 2
        expr = f"255*clip({r}-hypot(X+0.5-{r},Y+0.5-{r})+0.5,0,1)"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", f"color=black:s={d}x{d}:d=1,format=gray",
                        "-vf", f"geq=lum='{expr}'", "-frames:v", "1", str(path)],
                       check=True, creationflags=NO_WINDOW)
    return path


def white_disc(d):
    """Create (once) a soft-edged white disc PNG of diameter d, drawn behind the face as a ring."""
    MASK_DIR.mkdir(parents=True, exist_ok=True)
    path = MASK_DIR / f"disc_{d}.png"
    if not path.exists():
        r = d / 2
        expr = f"255*clip({r}-hypot(X+0.5-{r},Y+0.5-{r})+0.5,0,1)"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", f"color=white:s={d}x{d}:d=1,format=rgba",
                        "-vf", f"geq=r=255:g=255:b=255:a='{expr}'", "-frames:v", "1", str(path)],
                       check=True, creationflags=NO_WINDOW)
    return path


def default_out_dir():
    """~/Videos/Screen Recordings (created on first Start), else F:/Screen Recordings."""
    videos = Path.home() / "Videos"
    if videos.exists():
        return str(videos / "Screen Recordings")
    if Path("F:/Screen Recordings").exists():
        return "F:/Screen Recordings"
    return str(Path.home() / "Screen Recordings")


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.mkv_path = None
        self.start_time = 0.0
        self.log_tail = []
        self.finisher = None
        self.closing = False
        self.saved = None
        self.phone = None
        self.procs = []
        self.progress = 0

        root.title("Screen Face Recorder")
        root.resizable(False, False)
        f = ttk.Frame(root, padding=14)
        f.grid()

        self.video_devs, self.audio_devs = list_devices()
        self.amf = has_amf()

        ttk.Label(f, text="Webcam").grid(row=0, column=0, sticky="w")
        self.cam = ttk.Combobox(f, values=["(none)", PHONE] + self.video_devs,
                                state="readonly", width=38)
        self.cam.set(self.video_devs[0] if self.video_devs else "(none)")
        self.cam.grid(row=0, column=1, columnspan=2, pady=3)
        self.cam.bind("<<ComboboxSelected>>", lambda e: self.on_cam_change())

        # Shown only when the phone camera is selected
        self.phone_box = ttk.Frame(f)
        self.phone_box.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.phone_url = ttk.Label(self.phone_box, font=("Segoe UI", 12, "bold"),
                                   foreground="#0a58ca")
        self.phone_url.grid(sticky="w")
        self.phone_state = ttk.Label(self.phone_box, foreground="gray", wraplength=380)
        self.phone_state.grid(sticky="w")
        self.show_phone_hint()

        ttk.Label(f, text="Microphone").grid(row=1, column=0, sticky="w")
        self.mic = ttk.Combobox(f, values=["(none)"] + self.audio_devs, state="readonly", width=38)
        self.mic.set(self.audio_devs[0] if self.audio_devs else "(none)")
        self.mic.grid(row=1, column=1, columnspan=2, pady=3)

        ttk.Label(f, text="Face size").grid(row=2, column=0, sticky="w")
        self.size = ttk.Combobox(f, values=list(CAM_SIZES), state="readonly", width=12)
        self.size.set("Medium")
        self.size.grid(row=2, column=1, sticky="w", pady=3)
        self.quality = ttk.Combobox(f, values=list(QUALITY), state="readonly", width=14)
        self.quality.set(next(iter(QUALITY)))
        self.quality.grid(row=2, column=2, sticky="e", pady=3)

        ttk.Label(f, text="Save to").grid(row=3, column=0, sticky="w")
        self.out_dir = tk.StringVar(value=default_out_dir())
        ttk.Entry(f, textvariable=self.out_dir, width=30).grid(row=3, column=1, pady=3)
        ttk.Button(f, text="…", width=3, command=self.browse).grid(row=3, column=2)

        self.btn = ttk.Button(f, text="● Start recording", command=self.toggle)
        self.btn.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 4))

        self.status = ttk.Label(f, text="Ready", foreground="gray")
        self.status.grid(row=5, column=0, columnspan=3)

        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- phone camera ----------
    def on_cam_change(self):
        if self.cam.get() != PHONE:
            self.show_phone_hint()
            return
        try:
            if not self.phone:
                self.phone = PhoneCam(MASK_DIR / "phone_placeholder.jpg")
            url = self.phone.start()
        except Exception as e:  # cert/port problems
            messagebox.showerror("Phone camera", str(e))
            self.cam.set("(none)")
            return
        self.phone_url.configure(text=f"On your phone open:  {url}")
        self.phone_box.grid()
        self.poll_phone()

    def show_phone_hint(self):
        self.phone_url.configure(text="")
        self.phone_state.configure(
            text='To use your phone as the camera, pick "Phone camera (Wi-Fi)" '
                 "under Webcam - its link will appear here.", foreground="gray")

    def poll_phone(self):
        if self.cam.get() != PHONE or self.closing:
            return
        ok, fps = self.phone.status()
        if ok:
            self.phone_state.configure(text=f"Phone connected - {fps} fps", foreground="green")
        else:
            self.phone_state.configure(
                text="Waiting for phone… (same Wi-Fi; accept the security warning, "
                     "then tap Start camera)", foreground="gray")
        self.root.after(1000, self.poll_phone)

    # ---------- UI helpers ----------
    def browse(self):
        d = filedialog.askdirectory(initialdir=self.out_dir.get())
        if d:
            self.out_dir.set(d)

    def set_inputs_state(self, state):
        for w in (self.cam, self.mic, self.size, self.quality):
            w.configure(state=state)

    def tick(self):
        if self.proc and self.proc.poll() is None:
            s = int(time.time() - self.start_time)
            self.status.configure(text=f"Recording  {s // 60:02d}:{s % 60:02d}", foreground="red")
            self.root.after(500, self.tick)

    # ---------- recording ----------
    # While recording, three light ffmpeg processes each write one source to its own
    # file: screen, face camera, mic. Every file is timestamped on one shared clock
    # (seconds since Start). On Stop, the files are kept as they are and
    # sync-info.txt is written; combining them is a separate step. Doing the overlay live made ffmpeg pause the screen grabber while waiting for
    # camera frames (choppy video), or forced sync errors; offline it is exact.

    def build_capture_cmds(self, parts):
        cam = self.cam.get()
        mic = self.mic.get()
        out_w, out_h, preset, qp = QUALITY[self.quality.get()]

        # Common time base: this moment, read on both clocks the sources use.
        base_qpc, base_epoch = qpc_seconds(), time.time()
        ff = ["ffmpeg", "-hide_banner", "-y", "-copyts"]
        cmds = {}

        # Screen: GPU capture; its timestamps start at 0, so add the start time once.
        cmds["screen"] = ff + [
            "-thread_queue_size", "64", "-f", "lavfi",
            "-i", f"ddagrab=framerate={FPS}:draw_mouse=1,"
                  f"setpts=PTS+(RTCSTART/1000000-{base_epoch:.6f})/TB",
            "-vf", f"hwdownload,format=bgra,scale={out_w}:{out_h}:flags=bicubic,format=nv12",
            *encoder_args(self.amf, preset, qp), "-fps_mode", "passthrough",
            str(parts["screen"])]

        if cam == PHONE:
            port = self.phone.open_feed()
            # Frames are stamped on arrival; shift back by the typical phone delay.
            cmds["cam"] = ff + [
                "-f", "mjpeg", "-use_wallclock_as_timestamps", "1",
                "-thread_queue_size", "512", "-i", f"tcp://127.0.0.1:{port}",
                "-c:v", "copy", "-output_ts_offset", f"{-(base_epoch + PHONE_LATENCY):.6f}",
                str(parts["cam"])]
        elif cam != "(none)":
            cmds["cam"] = ff + [
                "-f", "dshow", "-thread_queue_size", "512", "-rtbufsize", "64M",
                "-video_size", "640x360", "-framerate", "30", "-i", f"video={cam}",
                "-vf", f"setpts=PTS-{base_qpc:.6f}/TB,crop=ih:ih",
                "-c:v", "mjpeg", "-q:v", "3", "-fps_mode", "passthrough",
                str(parts["cam"])]

        if mic != "(none)":
            cmds["audio"] = ff + [
                "-f", "dshow", "-thread_queue_size", "1024", "-audio_buffer_size", "50",
                "-i", f"audio={mic}", "-af", f"asetpts=PTS-{base_qpc:.6f}/TB",
                "-c:a", "aac", "-b:a", "160k", str(parts["audio"])]
        return cmds

    def toggle(self):
        if self.proc and self.proc.poll() is None:
            self.stop()
        else:
            self.start()

    def start(self):
        out_dir = Path(self.out_dir.get())
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        self.rec_dir = out_dir / stamp
        self.rec_dir.mkdir(parents=True, exist_ok=True)
        self.parts = {"screen": self.rec_dir / "screen.mkv",
                      "cam": self.rec_dir / "face.mkv",
                      "audio": self.rec_dir / "audio.mka"}
        self.job_quality, self.job_size = self.quality.get(), self.size.get()
        self.job_cam = self.cam.get()
        cmds = self.build_capture_cmds(self.parts)
        self.have = set(cmds)
        self.log_tail = []
        self.procs = []
        for name in ("audio", "cam", "screen"):  # slowest-to-open first
            if name not in cmds:
                continue
            p = subprocess.Popen(cmds[name], stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, text=True, creationflags=NO_WINDOW)
            threading.Thread(target=self.read_log, args=(p,), daemon=True).start()
            self.procs.append(p)
        self.proc = self.procs[-1]  # the screen process
        self.start_time = time.time()
        self.btn.configure(text="■ Stop recording")
        self.set_inputs_state("disabled")
        self.root.after(2500, self.check_alive)
        self.tick()

    def read_log(self, p):
        for line in p.stderr:
            self.log_tail = (self.log_tail + [line.rstrip()])[-15:]

    def check_alive(self):
        if not self.btn["text"].startswith("■") or (self.finisher and self.finisher.is_alive()):
            return
        if any(p.poll() is not None for p in self.procs):
            for p in self.procs:
                if p.poll() is None:
                    p.kill()
            self.reset_ui("Failed to start")
            messagebox.showerror("ffmpeg error", "\n".join(self.log_tail[-8:]))

    def stop(self):
        """Stop capturing; finishing the files and writing sync-info.txt happen on a background thread."""
        if self.finisher and self.finisher.is_alive():
            return
        self.btn.configure(state="disabled")
        self.progress = 0
        self.status.configure(text="Finishing…", foreground="gray")
        for p in self.procs:
            try:
                p.stdin.write("q")
                p.stdin.flush()
            except OSError:
                pass
        self.finisher = threading.Thread(target=self.finish, daemon=True)
        self.finisher.start()
        self.root.after(300, self.poll_finish)

    def poll_finish(self):
        """Runs on the UI thread; shows progress without touching Tk from the worker."""
        if self.closing:
            return
        if self.finisher.is_alive():
            self.status.configure(text="Saving…", foreground="gray")
            self.root.after(300, self.poll_finish)
        else:
            self.reset_ui(f"Saved: {self.saved.name}")

    @staticmethod
    def probe(path, entry):
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", f"format={entry}",
                            "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, creationflags=NO_WINDOW)
        try:
            return float(r.stdout.strip())
        except ValueError:
            return None

    def finish(self):
        for p in self.procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        if self.phone:
            self.phone.close_feed()

        have = [k for k in ("screen", "cam", "audio")
                if k in self.have and self.parts[k].exists() and self.parts[k].stat().st_size > 0]
        self.write_sync_info(have)
        self.saved = self.rec_dir

    def write_sync_info(self, have):
        """Describe the raw files so whoever combines them can line them up exactly."""
        info = {k: (self.probe(self.parts[k], "start_time"), self.probe(self.parts[k], "duration"))
                for k in have}
        out_w, out_h, _, _ = QUALITY[self.job_quality]
        d = round(out_h * CAM_SIZES[self.job_size]) // 2 * 2
        ring = max(3, round(out_h / 180))
        margin = round(out_h / 36)
        desc = {"screen": f"screen recording, {out_w}x{out_h}, {FPS} fps, H.264",
                "cam": "face camera, square crop, MJPEG"
                       + (" (phone over Wi-Fi)" if self.job_cam == PHONE else f" ({self.job_cam})"),
                "audio": "microphone, AAC"}
        lines = ["Screen Face Recorder - raw recording", "",
                 "FILES", "-----"]
        for k in have:
            st, du = info[k]
            lines.append(f"{self.parts[k].name:12s} {desc[k]}")
            # For these files ffprobe's "duration" is the end time on the shared clock.
            lines.append(f"{'':12s} runs from {st:.3f} s to {du:.3f} s" if st is not None and du
                         is not None else f"{'':12s} (timing unreadable)")
        lines += ["", "SYNC", "----",
                  "All files' timestamps are on ONE shared clock (seconds since Start was",
                  "pressed). Line them up by their own timestamps - do NOT start each file",
                  "at 0, or voice/face/screen will drift apart by the differences below.", ""]
        if info.get("screen", (None,))[0] is not None:
            s0 = info["screen"][0]
            for k in have:
                if k != "screen" and info[k][0] is not None:
                    lines.append(f"{self.parts[k].name} starts {info[k][0] - s0:+.3f} s "
                                 "relative to screen.mkv")
        lines += ["",
                  "With ffmpeg, add -copyts before the inputs to keep these timestamps.",
                  "Measured on this PC: mic and camera are each roughly 0.1-0.25 s late",
                  "versus the screen, so the voice may need a small manual nudge.",
                  "", "INTENDED LAYOUT", "---------------",
                  f"Face as a circle {d} px wide with a {ring} px white ring, bottom-left,",
                  f"{margin} px from the left and bottom edges of the screen video."]
        try:
            (self.rec_dir / "sync-info.txt").write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            pass

    def reset_ui(self, msg):
        self.btn.configure(text="● Start recording", state="normal")
        self.set_inputs_state("readonly")
        self.status.configure(text=msg, foreground="green" if msg.startswith("Saved") else "red")

    def on_close(self):
        recording = self.proc and self.proc.poll() is None and not (
            self.finisher and self.finisher.is_alive())
        if recording and not messagebox.askyesno("Recording", "Stop recording and quit?"):
            return
        self.closing = True
        if recording:
            self.stop()
        if self.finisher and self.finisher.is_alive():
            self.status.configure(text="Saving…")
            self.root.update()
            self.finisher.join()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
