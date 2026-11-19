# Screen Face Recorder

Records your screen, your phone's camera (as a face cam) and your mic as **separate, perfectly synced files**, so the face cam never lags.

## Why

On my setup, merging the face cam into the screen live (in OBS) made the face cam lag behind. This tool doesn't merge anything while recording. Each source goes to its own file, and all files are timestamped on **one shared clock** (seconds since you pressed Start). You combine them afterwards, and they line up exactly.

## Requirements

- Windows 10 or 11
- Python 3 with Tkinter (included in the python.org installer)
- `ffmpeg` and `ffprobe` on your PATH
- [Git for Windows](https://git-scm.com/download/win), only for the phone camera (the app uses its `openssl.exe` to make an HTTPS certificate)
- For the phone camera: the phone and PC on the same Wi-Fi
- AMD GPU optional: the screen is encoded with `h264_amf` if it's available, otherwise with `libx264`

## Run

```
git clone https://github.com/chaudharyShub/Screen-Face-recorder.git
cd Screen-Face-recorder
python recorder.py
```

Or double-click `Start Recorder.bat` (starts it without a console window).

Pick your webcam, microphone, face size, quality (1080p or 720p) and save folder, then press **Start recording**. The default save folder is `~\Videos\Screen Recordings`.

## Using your phone as the face cam

1. Under **Webcam**, pick **Phone camera (Wi-Fi)**. A link like `https://192.168.1.20:8443` appears in the app.
2. Open that link on your phone.
3. Accept the security warning. The certificate is self-signed, which is expected; phones only allow camera access over HTTPS.
4. Tap **Start camera**. The app shows "Phone connected".
5. Keep the page open on your phone while recording.

Windows may ask to allow Python through the firewall the first time. Allow it on private networks.

## Output

Each recording gets its own folder, e.g. `Screen Recordings\2026-11-20 14-30-00\`:

| File | What it is |
|---|---|
| `screen.mkv` | the screen, H.264, 30 fps |
| `face.mkv` | the face camera, square, MJPEG |
| `audio.mka` | the microphone, AAC |
| `sync-info.txt` | when each file starts and ends on the shared clock, plus the intended layout (face as a circle with a white ring, bottom-left, with exact pixel sizes) |

## Combining the files

Use `-copyts` so ffmpeg keeps each file's own timestamps instead of starting every file at 0. Run this inside a recording folder. It puts the face in a circle with a white ring at the bottom-left. The sizes match **1080p + Medium** (324 px circle, 6 px ring, 30 px margin). For other settings, use the numbers from `sync-info.txt`.

```
ffmpeg -copyts -i screen.mkv -i face.mkv -i audio.mka -filter_complex "[1:v]scale=324:324,format=yuva420p,geq=lum='if(gt(hypot(X-162,Y-162),156),255,p(X,Y))':cb='if(gt(hypot(X-162,Y-162),156),128,p(X,Y))':cr='if(gt(hypot(X-162,Y-162),156),128,p(X,Y))':a='255*lt(hypot(X-162,Y-162),162)'[face];[0:v][face]overlay=30:H-h-30:eof_action=pass[v]" -map "[v]" -map 2:a -c:v libx264 -crf 20 -c:a aac combined.mp4
```

In that filter, `162` is half the circle size and `156` is half the circle size minus the ring width.

You can also hand the files and `sync-info.txt` to a video editor or an AI tool.

## Limitations

- Windows only (it uses dshow, ddagrab and Windows clock APIs).
- The files are combined in a separate step, not while recording.
- The phone must be on the same Wi-Fi as the PC.
- The mic and camera can be about 0.1–0.25 s late compared with the screen (this is also noted in `sync-info.txt`), so the voice may need a small manual nudge.

## License

MIT, see [LICENSE](LICENSE).
