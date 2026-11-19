"""Phone camera over Wi-Fi.

The phone opens https://<pc-ip>:8443, grants camera access, and POSTs square JPEG
frames to /frame. The recorder's ffmpeg reads a steady 30 fps MJPEG stream from a
local TCP port that this module feeds with the latest phone frame (or a placeholder
while the phone isn't connected), so phone hiccups never stall the recording.
"""
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
HTTPS_PORT = 8443
FRAME_SIZE = 480  # phone sends FRAME_SIZE x FRAME_SIZE square JPEGs
FEED_FPS = 30
CERT_DIR = Path(__file__).resolve().parent / "certs"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Face Cam</title>
<style>
 body{margin:0;background:#111;color:#eee;font:16px system-ui,sans-serif;display:flex;
      flex-direction:column;align-items:center;gap:14px;padding:20px}
 .bubble{width:70vw;max-width:340px;aspect-ratio:1;border-radius:50%;overflow:hidden;
         border:4px solid #fff;background:#222}
 video{width:100%;height:100%;object-fit:cover}
 .mirror{transform:scaleX(-1)}
 button{font-size:18px;padding:12px 22px;border:0;border-radius:10px;background:#e33;color:#fff}
 button.sec{background:#444}
 #st{color:#aaa;min-height:1.4em;text-align:center}
</style></head><body>
<div class="bubble"><video id="v" class="mirror" playsinline muted autoplay></video></div>
<button id="go">Start camera</button>
<button id="flip" class="sec" hidden>Switch camera</button>
<div id="st">Tap "Start camera". Keep this page open while recording.</div>
<canvas id="c" width="SIZE" height="SIZE" hidden></canvas>
<script>
const v=document.getElementById('v'), c=document.getElementById('c'), ctx=c.getContext('2d');
const st=document.getElementById('st'), go=document.getElementById('go'), flip=document.getElementById('flip');
let facing='user', stream=null, running=false, sent=0, lock=null;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

async function openCam(){
  if(stream) stream.getTracks().forEach(t=>t.stop());
  stream=await navigator.mediaDevices.getUserMedia({audio:false,
    video:{facingMode:facing,width:{ideal:1280},height:{ideal:720},frameRate:{ideal:30}}});
  v.srcObject=stream; v.classList.toggle('mirror',facing==='user');
  await v.play();
}
async function keepAwake(){
  try{ lock=await navigator.wakeLock.request('screen'); }catch(e){}
}
document.addEventListener('visibilitychange',()=>{ if(document.visibilityState==='visible'&&running) keepAwake(); });

async function loop(){
  let t0=performance.now();
  while(running){
    const t=performance.now();
    if(v.videoWidth){
      const w=v.videoWidth,h=v.videoHeight,s=Math.min(w,h);
      ctx.drawImage(v,(w-s)/2,(h-s)/2,s,s,0,0,SIZE,SIZE);
      const blob=await new Promise(r=>c.toBlob(r,'image/jpeg',0.8));
      try{
        const r=await fetch('/frame',{method:'POST',body:blob,cache:'no-store'});
        if(r.ok) sent++;
      }catch(e){ st.textContent='Connection to PC lost - retrying…'; await sleep(500); continue; }
    }
    if(performance.now()-t0>1000){
      st.textContent='Connected to PC - sending '+sent+' fps';
      sent=0; t0=performance.now();
    }
    const dt=performance.now()-t; if(dt<33) await sleep(33-dt);
  }
}
go.onclick=async()=>{
  try{
    await openCam(); await keepAwake();
    go.hidden=true; flip.hidden=false; running=true; loop();
  }catch(e){ st.textContent='Camera error: '+e.message; }
};
flip.onclick=async()=>{ facing=facing==='user'?'environment':'user'; try{await openCam();}catch(e){st.textContent='Camera error: '+e.message;} };
</script></body></html>
""".replace("SIZE", str(FRAME_SIZE))


def local_ip():
    """IP of the interface used for the LAN (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def find_openssl():
    candidates = [shutil.which("openssl"),
                  r"C:\Program Files\Git\mingw64\bin\openssl.exe",
                  r"C:\Program Files\Git\usr\bin\openssl.exe"]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def ensure_cert():
    """Self-signed cert (phones only allow camera access over HTTPS)."""
    cert, key = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    fresh = cert.exists() and key.exists() and time.time() - cert.stat().st_mtime < 300 * 86400
    if fresh:
        return cert, key
    openssl = find_openssl()
    if not openssl:
        raise RuntimeError("OpenSSL not found (install Git for Windows) - needed for HTTPS.")
    CERT_DIR.mkdir(exist_ok=True)
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "365",
                    "-subj", "/CN=Screen Face Recorder"],
                   check=True, capture_output=True, creationflags=NO_WINDOW)
    return cert, key


def make_placeholder(path):
    if not path.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", f"color=0x303030:s={FRAME_SIZE}x{FRAME_SIZE}:d=1",
                        "-frames:v", "1", "-q:v", "5", str(path)],
                       check=True, creationflags=NO_WINDOW)
    return path.read_bytes()


class PhoneCam:
    def __init__(self, placeholder_path):
        self.latest = make_placeholder(placeholder_path)
        self.last_frame_at = 0.0
        self._times = []
        self.httpd = None
        self.url = None
        self._feed_stop = threading.Event()
        self._feed_sock = None

    # ---------- HTTPS server for the phone ----------
    def start(self):
        if self.httpd:
            return self.url
        cert, key = ensure_cert()
        cam = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive: one TLS handshake, not one per frame

            def log_message(self, *a):
                pass

            def do_GET(self):
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(n) if 0 < n < 5_000_000 else b""
                if self.path == "/frame" and data[:2] == b"\xff\xd8":
                    cam._on_frame(data)
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        httpd = ThreadingHTTPServer(("0.0.0.0", HTTPS_PORT), Handler)
        httpd.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.httpd = httpd
        self.url = f"https://{local_ip()}:{HTTPS_PORT}"
        return self.url

    def _on_frame(self, data):
        now = time.time()
        self.latest = data
        self.last_frame_at = now
        self._times = [t for t in self._times if now - t < 1.0] + [now]

    def status(self):
        if time.time() - self.last_frame_at > 2:
            return False, 0
        return True, len([t for t in self._times if time.time() - t < 1.0])

    # ---------- local MJPEG feed for ffmpeg ----------
    def open_feed(self):
        """Listen on a free localhost port; ffmpeg connects and gets a steady MJPEG stream."""
        self.close_feed()
        self._feed_stop = threading.Event()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self._feed_sock = srv
        threading.Thread(target=self._feed, args=(srv, self._feed_stop), daemon=True).start()
        return srv.getsockname()[1]

    def _feed(self, srv, stop):
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            next_t = time.perf_counter()
            while not stop.is_set():
                try:
                    conn.sendall(self.latest)
                except OSError:
                    return
                # Keep a steady 30 fps average: after a short hiccup, send the
                # missed frames quickly to catch up.
                next_t += 1 / FEED_FPS
                delay = next_t - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -1.0:  # badly stalled; resync rather than burst for ages
                    next_t = time.perf_counter()

    def close_feed(self):
        self._feed_stop.set()
        if self._feed_sock:
            try:
                self._feed_sock.close()
            except OSError:
                pass
            self._feed_sock = None
