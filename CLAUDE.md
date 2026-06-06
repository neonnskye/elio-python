# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All Python commands use `uv` (managed in [pyproject.toml](pyproject.toml)).

```bash
# Install dependencies
uv sync

# Run the receiver
uv run receiver.py

# Check DeepSeek API connectivity (direct, not via OpenRouter)
uv run tools/test_deepseek.py

# Convert WAV to C header (for ESP32 chime data)
uv run tools/wav_to_header.py input.wav output.h var_name --rate 16000
```

## Project Overview

Python backend for the **Elio** voice assistant. Receives real-time audio from an ESP32 over Wi-Fi UDP,
runs Silero VAD, Groq STT, and streams LLM responses through TTS playback over I2S.
Firmware source (ESP32 Arduino, board: NodeMCU-32S) lives in a separate repository.

- **Python:** requires >= 3.13; managed with `uv`
- **Dependencies:** `groq`, `openai`, `scipy`, `sounddevice`, `torch` (CPU via pytorch-cpu index), `torchaudio`, `paho-mqtt`, `piper-tts`, `zeroconf`, `opencv-python`
- **Runtime deps:** two API keys (`GROQ_API_KEY`, `DEEPSEEK_API_KEY`) — set as environment variables

## Hardware Context

This repo is the **Python backend** — firmware source lives elsewhere. Key context for understanding the audio pipeline:

- **Sample rate:** 16 000 Hz (must match ESP32 firmware)
- **Input format:** 512 uint16 samples per UDP packet on port 12345
- **Output to ESP32:** int16 PCM mono UDP on port 12347, paced to real-time
- **Control to ESP32:** MQTT messages (`elio/wake` for wake word trigger, `elio/ctrl` for "processing"/"stop" commands)
- **Broker:** Mosquitto (or any MQTT broker) running on `127.0.0.1:1883`

## Code Structure

- [receiver.py](receiver.py) — entry point; multi-threaded UDP receiver with VAD/STT/LLM/TTS pipeline
- [pyproject.toml](pyproject.toml) — Python project config (managed with `uv`)
- [tools/](tools/) — utility scripts for development (see below)
- [docs/](docs/) — documentation (MQTT topic spec, etc.)
- [models/](models/) — local models: Piper TTS ONNX + Silero VAD (cloned repo)
- [system.md](system.md) — LLM system prompt (read at startup)
- [camera.py](camera.py) — Camera module for face tracking (runs on Raspberry Pi with camera)
- [logger.py](logger.py) — Minimal MQTT logger utility (subscribes to all topics)
- `.venv/` — Python virtual environment (managed by `uv`, not edited manually)

### Utilities (`tools/`)

| Script | Purpose |
|--------|---------|
| [wav_to_header.py](tools/wav_to_header.py) | Convert WAV files to C headers (for ESP32 chime data) |
| [test_deepseek.py](tools/test_deepseek.py) | Quick connectivity test for DeepSeek API |
| [data_prep.sh](tools/data_prep.sh) | Shell script for audio dataset preparation |

## Audio Pipeline

This repo implements a multi-threaded voice pipeline that runs on the PC (see [receiver.py](receiver.py) for all thread definitions):

```
ESP32 mic → UDP (port 12345) → receive_loop → [packet_queue → local speaker (passthrough)]
                                              → [vad_queue → VAD → STT → LLM → TTS → speaker output]
                                                                                        ↓
                                                                       ESP32 I2S or local sounddevice
```

### Pipeline Threads (6 daemon threads + MQTT callback)

| Thread | Purpose | Key Config |
|--------|---------|------------|
| `receive_loop` | Receive UDP audio, decode uint16→float32, push to dual queues | `UDP_PORT`, `SAMPLES_PER_PKT` |
| `vad_accumulator_loop` | Run Silero VAD on audio, build speech segments | `SILERO_VAD_THRESHOLD`, `CAPTURE_TIMEOUT_S` |
| `transcription_loop` | Groq STT on completed segments | `GROQ_MODEL`, `STT_TIMEOUT_S` |
| `llm_loop` | Stream LLM response via DeepSeek API, split sentences | `LLM_MODEL`, `LLM_SYSTEM_PROMPT` (from system.md) |
| `tts_loop` | Piper TTS (local), resample 22050→16 kHz, queue for dispatch | `PIPER_MODEL_PATH` |
| `audio_dispatch_loop` | Drain TTS audio to ESP32 UDP or local sounddevice | `AUDIO_OUTPUT`, `ESP32_IP` |
| `on_mqtt_message` (MQTT callback) | Handles wake word (`elio/wake`) — replaces old `control_listener` thread | `MQTT_BROKER`, `TOPIC_WAKE` |

### Wake Word State Machine

The `ListenState` enum (in Python) drives the entire pipeline:

When the Edge Impulse classifier detects the wake word (label `"elio"` > 0.6), the ESP32 publishes a message to MQTT topic `elio/wake`. The Python receiver's `on_mqtt_message` callback picks this up and drives a `ListenState` state machine:

| State | Description |
|-------|-------------|
| `IDLE` | Waiting for wake word signal. Audio is streamed but not transcribed. |
| `SKIP_WAKEWORD_BLEED` | Discards `BLEED_SKIP_PACKETS` (~768 ms) of audio after the wake word — covers utterance bleed + begin chime duration. |
| `CAPTURING` | Actively recording the user's command. VAD accumulator builds a speech segment. |
| `TRANSCRIBING` | Groq STT (whisper-large-v3) is transcribing; new captures are blocked. |
| `RESPONDING` | LLM is generating a response / TTS is synthesizing speech; pipeline busy. |

Flow: `IDLE` → (wake packet received) → `SKIP_WAKEWORD_BLEED` → `CAPTURING` → (silence or max segment) → `TRANSCRIBING` → `RESPONDING` → (TTS playback ends) → `IDLE`.

When entering `CAPTURING`, the PC publishes `"processing"` to `elio/ctrl` to tell the ESP32 to start its processing LED/indicator. When TTS audio is ready, `"stop"` is published to turn it off. On TTS failure or pipeline abort, `"stop"` is also published to reset the ESP32's audio state.

A `CAPTURE_TIMEOUT_S` (3 s) timer starts when entering `CAPTURING`; if no speech is detected by **Silero VAD** within that window, the state resets to `IDLE` (false-positive guard).

### ESP32 ↔ PC Contract

The ESP32 firmware (separate repo) sends UDP audio at 16 kHz, 512 uint16 samples per packet on port 12345.
Control flows over **MQTT** (broker at `127.0.0.1:1883`): wake word triggers on `elio/wake`, LED/state commands on `elio/ctrl` with payloads `"processing"` or `"stop"`.
TTS audio is sent back to the ESP32 on port 12347 as int16 PCM mono, paced to real-time.
ESP32 IP is resolved via mDNS (`esp32-audio.local`) using zeroconf at startup.

See the [Python config table below](#python-configuration-receiverpy-constants) for port/address constants.

### Python Backend

The receiver ([receiver.py](receiver.py)) is a multi-threaded design with 6 daemon threads plus an MQTT callback:

- **`receive_loop`** — receives UDP datagrams, decodes samples (uint16 → float32, DC-offset removed), pushes to `packet_queue` (playback, noise-gated) and `vad_queue` (original audio for VAD). Drops oldest on overflow.
- **`on_mqtt_message`** (MQTT callback, runs via `mqtt_client.loop_start()`) — subscribes to `elio/wake` and drives the `ListenState` state machine when a wake word message arrives. Includes a 1.5 s cooldown to debounce repeated triggers. Replaces the old `control_listener` thread.
- **`vad_accumulator_loop`** — builds speech segments from `vad_queue` when in `CAPTURING` state using **Silero VAD** (PyTorch neural model, loaded from local `models/silero-vad/` directory, `SILERO_VAD_THRESHOLD=0.5`). Pushes completed segments to `transcribe_queue`. Includes false-positive timeout (`CAPTURE_TIMEOUT_S`). Publishes `"processing"` via MQTT to ESP32 on segment ready.
- **`transcription_loop`** — calls **Groq STT** (`whisper-large-v3`) on completed segments with a `STT_TIMEOUT_S` (15 s) guard thread. Segments ≤3 words are discarded (wake-word bleed filter). Publishes `"processing"` via MQTT to trigger the processing LED during the STT/LLM gap.
- **`llm_loop`** — receives transcripts from `transcription_loop` via `llm_queue`, streams a response from **DeepSeek** (`deepseek-v4-flash`, direct API at `api.deepseek.com`) via the OpenAI SDK. Applies streaming sentence splitting (avoids splitting on abbreviations/initials), markdown stripping, and pushes each complete sentence to `tts_queue`. Maintains conversation history (capped at `CONVERSATION_HISTORY_MAX_TURNS=20`). Timeout safety: `LLM_TOKEN_TIMEOUT_S` (8 s between tokens) + `LLM_TOTAL_TIMEOUT_S` (45 s total). System prompt loaded from `system.md`.
- **`tts_loop`** — calls **Piper TTS** locally (`models/en_US-hfc_female-medium.onnx`, PiperVoice) on queued LLM sentences with a `TTS_TIMEOUT_S` (20 s) guard. Resamples Piper's 22050 Hz output to 16 kHz via `scipy.signal.resample_poly`, normalizes peak, applies 0.6 volume factor, then appends to `audio_queue` for the dispatch thread. Publishes `"stop"` via MQTT on TTS failure.
- **`audio_dispatch_loop`** — drains `audio_queue` and routes completed TTS audio to the configured output(s): `"local"` (sounddevice callback), `"esp32"` (UDP to ESP32 on port 12347, paced to real-time with deadline-based timing), or `"both"`. Returns to `IDLE` state after sending.
- **`audio_callback`** — `sounddevice` callback. In normal mode drains `packet_queue` (mic passthrough). When `is_responding=True`, drains `response_queue` (TTS audio from LLM). Handles leftover-sample carry between callbacks. A `None` sentinel in the queue signals end of TTS playback.

Requires two environment variables: `GROQ_API_KEY` and `DEEPSEEK_API_KEY`.

On startup, a background warmup thread primes the DeepSeek KV cache with a dummy request to reduce first-interaction latency.

A **Mosquitto** (or any MQTT broker) must be running on `127.0.0.1:1883` before receiver.py starts. The ESP32 firmware also connects to this broker for wake word and control signals.

#### Python Configuration (`receiver.py` constants)

| Constant | Default | Description |
|----------|---------|-------------|
| `UDP_PORT` | `12345` | Must match firmware `UDP_PORT` |
| `SAMPLE_RATE` | `16000` | Must match firmware sample rate |
| `SAMPLES_PER_PKT` | `512` | Must match firmware `SAMPLES_PER_PKT` |
| `PREBUFFER_PKTS` | `3` | Packets to queue before playback starts (~96 ms) |
| `MAX_QUEUE_LEN` | `10` | Max queued packets before dropping oldest (~320 ms) |
| `NOISE_GATE` | `0` | RMS threshold below which a packet is silenced (0 = off) |
| `SILERO_VAD_THRESHOLD` | `0.5` | Silero VAD speech probability threshold |
| `RECORDING_MODE` | `False` | Set True for dataset collection passthrough (disables all pipeline threads, mic→speaker only) |
| `AUDIO_OUTPUT` | `"esp32"` | Audio routing: `"local"`, `"esp32"`, or `"both"` |
| `ESP32_MDNS_HOST` | `"esp32-audio"` | mDNS hostname broadcast by the ESP32 (resolved via zeroconf) |
| `ESP32_IP` | — | ESP32 IP resolved from `ESP32_MDNS_HOST` at startup |
| `MQTT_BROKER` | `"127.0.0.1"` | MQTT broker address (Mosquitto must be running) |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `TOPIC_WAKE` | `"elio/wake"` | MQTT topic for wake word trigger from ESP32 |
| `TOPIC_CTRL` | `"elio/ctrl"` | MQTT topic for PC→ESP32 control signals |
| `GROQ_MODEL` | `"whisper-large-v3"` | Groq STT model for transcription |
| `PIPER_MODEL_PATH` | `models/en_US-hfc_female-medium.onnx` | Piper local TTS model |
| `TTS_PCM_RATE` | `22050` | Piper TTS output sample rate (resampled to 16 kHz) |
| `LLM_MODEL` | `"deepseek-v4-flash"` | DeepSeek model for LLM responses (direct API at api.deepseek.com) |
| `VAD_SILENCE_MS` | `500` | Trailing silence required to end a speech segment |
| `VAD_MIN_SPEECH_MS` | `400` | Minimum speech length; shorter segments are discarded |
| `MAX_SEGMENT_S` | `10` | Hard cap — force transcribe even if no silence detected |
| `CAPTURE_TIMEOUT_S` | `3` | Seconds of silence after wake word before treating as false positive |
| `CONVERSATION_HISTORY_MAX_TURNS` | `20` | Max message objects in LLM history (~10 exchanges) |
| `BLEED_SKIP_PACKETS` | `16` | Packets to discard after wake word (~768 ms: covers utterance bleed + begin chime) |
| `STT_TIMEOUT_S` | `15` | Max seconds to wait for Groq STT response |
| `LLM_TOKEN_TIMEOUT_S` | `8` | Max seconds between tokens in LLM stream |
| `LLM_TOTAL_TIMEOUT_S` | `45` | Hard cap on total LLM response time |
| `TTS_TIMEOUT_S` | `20` | Max seconds to wait for Piper TTS response |

#### Running the receiver

Requires two API keys set as environment variables. Also requires an **MQTT broker** (e.g., Mosquitto) running on `127.0.0.1:1883`:

```bash
# Required: set your API keys before running
export GROQ_API_KEY="gsk_..."
export DEEPSEEK_API_KEY="sk-..."

# Install dependencies (requires uv)
uv sync

# Ensure Mosquitto is running (or another MQTT broker on 127.0.0.1:1883)
# On Linux: sudo systemctl start mosquitto

# Run
uv run receiver.py
```

On Windows (PowerShell):
```powershell
$env:GROQ_API_KEY="gsk_..."
$env:DEEPSEEK_API_KEY="sk-..."
uv run receiver.py
```

The ESP32 IP is resolved automatically via **mDNS** (zeroconf) from `ESP32_MDNS_HOST`. No static IP config needed.

The `AUDIO_OUTPUT` constant in `receiver.py` controls audio routing: `"esp32"` (default — sends TTS to ESP32 for I2S playback), `"local"` (plays on the PC running receiver.py), or `"both"`.

ESP32 streams live diagnostic counts to serial: `Sent: N | Failed: N`. A rising `Failed` count indicates network or send-path issues.

**NOTE:** The old control listener thread (UDP ports 12346/12348) has been replaced by MQTT on `elio/wake` and `elio/ctrl`. The separate UDP control ports are no longer used.

## Troubleshooting

### Python receiver stays at "Waiting for N packets to pre-buffer..."
1. Verify ESP32 and PC are on the same subnet (ESP32 streams to `PC_IP`, not broadcast)
2. Check firewall allows UDP port 12345 inbound
3. Confirm `PC_IP` in the ESP32 firmware matches the machine running `receiver.py`
4. ESP32 firmware shows `Sent:` and `Failed:` counters on serial — `Failed` incrementing indicates send errors

### Receiver boots but no audio / errors
- Confirm both `GROQ_API_KEY` and `DEEPSEEK_API_KEY` are set before running
- Ensure **Mosquitto** (or another MQTT broker) is running: `sudo systemctl status mosquitto`
- Check the ESP32 resolves `esp32-audio.local` on the network (or adjust `ESP32_MDNS_HOST`)
- Check `AUDIO_OUTPUT` is set to `"local"` if no ESP32 is available for testing
- Set `RECORDING_MODE = True` to test mic-to-speaker passthrough without any API calls

### "Could not connect to broker" or MQTT errors
- Run `sudo systemctl start mosquitto` (Linux) or install Mosquitto on Windows
- Verify nothing else is bound to port 1883
- The ESP32 and PC must both connect to the same MQTT broker

### Wake word not triggering
- Check ESP32 can reach the MQTT broker (topic `elio/wake`)
- Run `uv run logger.py` to monitor all MQTT messages and verify `elio/wake` arrives
- The old UDP control port mechanism (12346/`0x01`) has been replaced by MQTT — ensure firmware is updated
