# Fleet Agent

The operator-laptop companion to the Capitan web server. It logs in to Captain as the operator, then lets
them command drones with a **joystick** or **voice** instead of the right-click menu, keeps device
**settings** in Captain's database, and registers new users with **face and voice** enrolment.

```
 operator laptop                                             drone companion computer
┌──────────────────────────────────────────┐              ┌────────────────────────────┐
│ browser ─► Agent web UI (127.0.0.1:8780) │              │ Capitan web server :8766   │
│              own HTML / CSS / JS         │              │                            │
│ Fleet Agent (Python) ───────────────────────────────────►  POST /auth/login          │
│   • joystick (pygame, background-safe)   │  session     │  WS   /ws/agent  (presence)│
│   • voice gate + grammar + confirm       │  cookie,     │  POST /command   (commands)│
│   • face / voiceprint engines            │  in memory   │  GET/PUT /agent/settings   │
│   • settings sync                        │              │  GET/PUT /agent/biometrics │
└──────────────────────────────────────────┘              └────────────────────────────┘
```

## Run it

```
# 1. Captain side (once): apply the agent-support patch to the Captain tree
cd <your repo root> && patch -p1 < captain-agent-support.patch

# 2. Laptop
pip install -r requirements.txt
python -m fleet_agent --captain http://<drone-ip>:8766
```
The console prints a link ending in `?k=<token>` and opens it. The token is per run; opening the plain
address without it is refused (see Security). `--captain` can also come from `CAPITAN_URL` or an
`agent.json` (the file Captain's `GET /agent/agent.json` produces) in the working directory or `~/.fleet_agent/`.

### Logs

The agent writes a rotating log to `~/.fleet_agent/fleet_agent.log` (on Windows,
`%USERPROFILE%\.fleet_agent\fleet_agent.log`) as well as the console. Set
`FLEET_AGENT_LOG_FILE` to use a different file path. The log includes speech
engine status, recognized transcripts and voice-command results, voice settings
changes, unlock outcomes, and requests to Captain. It does not record audio,
passwords, or microphone device IDs. Spoken transcripts and custom voice-command
phrases are included to help diagnose recognition and matching, so treat the log
as private and review it before sharing. The log rotates at about 5 MB and keeps
four backups.

### Voice trace file

Every speech-to-text engine run is also recorded as JSON lines in `~/.fleet_agent/voice_trace.jsonl`
(on Windows `%USERPROFILE%\.fleet_agent\voice_trace.jsonl`) — **enabled by default since v0.x**, so you can
always check after a failed session what the engine reported (listening / stopped / error / fallback events,
raw transcripts, and whether each transcript matched a registered command). Set `FLEET_AGENT_TRACE_FILE` to a
different path, or explicitly to an empty string to disable it. The file rotates at 5 MB (`.jsonl.1`). It
contains transcripts and custom command phrases — treat it like the main log. The active path is also shown in
`GET /api/status` → `voice.trace_file` and logged at startup (`voice-trace file enabled path=…`).

## What the Captain header icons do (no new Captain UI code)

| Icon | Behaviour | Where it comes from |
|---|---|---|
| `agent-link-status` | green while logged in with the presence socket up | `AGENT_EVENT.CONNECTED/DISCONNECTED` — already in `routes/agent.py` + `app.js` |
| `joystick-status` | green while a pad is physically detected | `AGENT_EVENT.JOYSTICK`, from the agent's state frames |
| `agent-cmd-status` | flashes when Captain accepts (green) or rejects (red) a command | `COMMAND_SENT` |
| `tts-status` | also flashes for **voice** commands | `app.js` already does this for `source === 'voice'` |

The only change to Captain's behaviour is in `server.py`: `/command` now passes the agent's `source_hint`
("voice" / "joystick") to `command_sent()`, so the voice flash actually happens. If you want `tts-status` to
flash for joystick commands too, change `source === 'voice'` to `source === 'voice' || source === 'joystick'`
at `app.js` line ~830.

## Tabs
- **Voice** — locked until unlocked (below). Wake word (default "captain") → one exact phrase →
  `land`, `return home`, `hold`, `pause/resume mission` run at once; `arm`, `disarm`, `takeoff` wait for "confirm"
  (or the Confirm button) for 8 s. "drone 2" picks a drone, otherwise the target selector is used.
  After **every** utterance the tab shows whether the transcript matched a command in your voice-command
  registry (e.g. the custom "arm"/"disarm" phrases) or not — matches are logged as *phrase matched registry
  entry*, misses as *did not match any registered custom command* with the reason shown in the activity line.
- **Joystick** — enabled only while a pad is detected. Live stick/button view; bind any button to an action.
  `arm/disarm/takeoff` need the button held (default 800 ms). **Nothing that starts motors is bound by default.**
- **Settings** — speech engine, language, microphone, wake word, re-lock time, unlock factors, camera; shows what
  Captain has saved and can restore or reset each section.
- **Register user** — Captain admins only. The signed-in role is shown in the header; operators must ask an
  admin to create accounts or enrol their face and voice.

## Voice unlock
Voice stays locked until the operator satisfies **every** factor they chose (default: Captain password):
`password` (re-checked against Captain), `voiceprint`, `face`. It re-locks after the idle time, on logout or
session expiry. Removing a factor while locked needs the Captain password; adding one requires an enrolled
template and an installed engine, so nobody can lock themselves out. Five failed attempts pause unlock for a minute.
The joystick needs no unlock: commands are only accepted while the hardware is physically present.

## Settings in Captain's database
`agent_settings (operator, scope, value_json)` — scopes `joystick`, `voice`, `camera`. Saved on every change,
restored on login, per operator, validated by `settings_schema.py` (unknown keys are dropped). The Captain
agent-settings patch must be installed in the running Captain server; if the agent reports HTTP 404 for
`GET /agent/settings`, restart Captain with that patch installed before saving settings. Biometric templates
live in `agent_biometrics`; only the owner reads their own, only an admin writes or revokes.

## Face and voice engines (optional, loaded if present)
- **Face**: `pip install opencv-python`, download `face_detection_yunet_2023mar.onnx` and
  `face_recognition_sface_2021dec.onnx` from the OpenCV model zoo into the project `models/` folder or
  `~/.fleet_agent/models/` (or set `FLEET_AGENT_YUNET_ONNX` / `FLEET_AGENT_SFACE_ONNX`). Face can be
  selected as an unlock factor only when this engine is available and an admin has enrolled your face.
- **Voice**: `pip install resemblyzer`.
If one is missing the UI says exactly what to install and the rest of the agent works.

## Adding your own speech-to-text engine
Create `www/js/stt/<name>.js`, call `registerEngine(name, {label, available, create})` (contract in
`stt/registry.js`), and import it in `tab_voice.js`. Engines only report transcripts; the agent decides what they mean.
A server-side engine can post to `/api/voice/transcript` directly. See `stt/local.js` for a complete example that
captures microphone audio in the browser and sends WAV chunks to the agent's `POST /api/stt/transcribe` endpoint.

## Offline speech recognition (local STT backup)
The Web Speech API in Chrome/Edge streams audio to the browser vendor's cloud service, so it fails without
internet ("speech engine offline"). As a backup the agent ships a **fully local pipeline**:

- Browser side: `www/js/stt/local.js` — records the mic, segments utterances with an energy VAD, downsamples to
  16 kHz mono WAV and posts each utterance to the agent. No internet involved.
- Agent side: `stt_local.py` (`LocalStt`) transcribes the WAV with either
  [Vosk](https://alphacephei.com/vosk/) (`pip install vosk`, model unpacked into `~/.fleet_agent/models/vosk`,
  override with `FLEET_AGENT_VOSK_MODEL`) or
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`pip install faster-whisper`, model name via
  `FLEET_AGENT_WHISPER_NAME`, default `base.en`).
- Endpoint: `POST /api/stt/transcribe` (login-gated, size-capped, runs off the event loop). `GET /api/status`
  reports `voice.local_stt = {backend, available, reason}` — if neither library/model is installed the reason
  says exactly what to install.

Select **"Local offline STT via agent"** in Settings → Speech engine once a backend is installed; both engines
appear in the dropdown because they self-register. Transcripts from either engine go through the same voice gate,
so command matching behaves identically offline.

## Security notes
- The agent only answers `127.0.0.1`/`localhost:<port>`, checks `Origin`, and needs the per-run token cookie —
  a web page open in the operator's browser (or DNS rebinding) cannot drive it.
- The Captain session token stays in the agent process; the browser never sees it.
- Face and voice are **not liveness-checked**: a photo or recording of the operator can match. Use them as a
  convenience on top of the Captain login. Thresholds are the libraries' documented starting values — calibrate on your hardware.
- Templates are embeddings (no photos/audio) but are stored unencrypted in the drone's SQLite — treat as personal data.
- Captain's `routes/users.py` still lets *any* logged-in account create accounts with any role (its own docstring
  says to gate it). The Register tab is admin-only, and the biometric endpoints enforce admin server-side, but
  `POST /auth/users` itself is only as strict as that file.

## Not done / not verified
- **Sticks do not fly the drone.** Captain's `ALLOWED_MAVLINK_COMMANDS` has no manual-control command; adding one is a
  separate, safety-critical design. Only buttons send (discrete commands).
- "Confirmed" means **Captain accepted and published** the command. The browser also waits for the flight controller's
  `CommandResult` via `/ws/monitor`; the agent deliberately doesn't open that socket.
- The agent can't see which drone is selected in the Captain map; it has its own target picker, filled from `GET /fleet/state`.
- The Web Speech engine needs internet in Chrome/Edge. A local offline backup exists — see
  *Offline speech recognition (local STT backup)* above; it still needs a Vosk/faster-whisper model installed
  on the laptop before first use.
- `server.py` mentions a `fleet_agent.py` that wasn't uploaded; this project is a fresh implementation — reconcile if one exists.
- Tested with stand-ins for the files that weren't uploaded (`WalConnection`, `fleet_bus`, `UserStore`, `command_parser`'s
  policy imports) and **no real joystick, browser speech/camera/microphone, face/voice models, or flight controller**.

## Tests
```
python -m pytest tests/test_units.py                                    # no Captain needed
CAPTAIN_SRC=<patched Captain tree> python -m pytest                   # + end-to-end against the real patched server.py
CAPTAIN_SRC=... UITEST_DIR=<dir with jsdom+esbuild> sh tests/run_ui_smoke.sh    # UI in jsdom (npm i jsdom esbuild)
```
