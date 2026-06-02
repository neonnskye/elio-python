# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All Python commands use `uv` (managed in [pyproject.toml](pyproject.toml)).

```bash
# Install dependencies
uv sync

# Run the receiver
uv run receiver.py

# Check OpenRouter connectivity
uv run tools/check_openrouter.py

# Verify PyTorch installation
uv run tools/check_torch.py

# Convert WAV to C header (for ESP32 chime data)
uv run tools/wav_to_header.py input.wav output.h var_name --rate 16000
```

## Project Overview

Python backend for the **Elio** voice assistant. Receives real-time audio from an ESP32 over Wi-Fi UDP,
runs Silero VAD, Groq STT, and streams LLM responses through TTS playback over I2S.
Firmware source (ESP32 Arduino, board: NodeMCU-32S) lives in a separate repository.

- **Python:** requires >= 3.13; managed with `uv`
- **Dependencies:** `groq`, `openai`, `scipy`, `sounddevice`, `torch` (CPU via pytorch-cpu index), `torchaudio`
- **Runtime deps:** two API keys (`GROQ_API_KEY`, `OPENROUTER_API_KEY`) — set as environment variables

## Hardware Context

This repo is the **Python backend** — firmware source lives elsewhere. Key context for understanding the audio pipeline:

- **Sample rate:** 16 000 Hz (must match ESP32 firmware)
- **Input format:** 512 uint16 samples per UDP packet on port 12345
- **Output to ESP32:** int16 PCM mono UDP on port 12347, paced to real-time
- **Control to ESP32:** `0x01` (wake trigger), `0x02` (start chime), `0x03` (stop chime) on port 12348/12346

## Code Structure

- [receiver.py](receiver.py) — entry point; multi-threaded UDP receiver with VAD/STT/LLM/TTS pipeline
- [pyproject.toml](pyproject.toml) — Python project config (managed with `uv`)
- [tools/](tools/) — utility scripts for development (see below)
- `.venv/` — Python virtual environment (managed by `uv`, not edited manually)

### Utilities (`tools/`)

| Script | Purpose |
|--------|---------|
| [wav_to_header.py](tools/wav_to_header.py) | Convert WAV files to C headers (for ESP32 chime data) |
| [check_openrouter.py](tools/check_openrouter.py) | Quick connectivity test for OpenRouter API |
| [check_torch.py](tools/check_torch.py) | Verify PyTorch/CUDA installation |
| [data_prep.sh](tools/data_prep.sh) | Shell script for audio dataset preparation |

## Audio Pipeline

This repo implements a multi-threaded voice pipeline that runs on the PC (see [receiver.py](receiver.py) for all thread definitions):

```
ESP32 mic → UDP (port 12345) → receive_loop → [packet_queue → local speaker (passthrough)]
                                              → [vad_queue → VAD → STT → LLM → TTS → speaker output]
                                                                                        ↓
                                                                       ESP32 I2S or local sounddevice
```

### Pipeline Threads (7 daemon threads)

| Thread | Purpose | Key Config |
|--------|---------|------------|
| `receive_loop` | Receive UDP audio, decode uint16→float32, push to dual queues | `UDP_PORT`, `SAMPLES_PER_PKT` |
| `control_listener` | Listen on `CTRL_PORT` (12346) for wake word (`0x01`) from ESP32 | `CTRL_PORT`, `WAKE_COOLDOWN_S` |
| `vad_accumulator_loop` | Run Silero VAD on audio, build speech segments | `SILERO_VAD_THRESHOLD`, `CAPTURE_TIMEOUT_S` |
| `transcription_loop` | Groq STT on completed segments | `GROQ_MODEL`, `STT_TIMEOUT_S` |
| `llm_loop` | Stream LLM response via OpenRouter, split sentences | `LLM_MODEL`, `LLM_SYSTEM_PROMPT` (in code) |
| `tts_loop` | OpenRouter TTS, resample 24→16 kHz, queue for dispatch | `TTS_MODEL`, `TTS_VOICE` |
| `audio_dispatch_loop` | Drain TTS audio to ESP32 UDP or local sounddevice | `AUDIO_OUTPUT`, `ESP32_IP` |

### Wake Word State Machine

The `ListenState` enum (in Python) drives the entire pipeline:

When the Edge Impulse classifier detects the wake word (label `"elio"` > 0.6), the ESP32 sends a 1-byte UDP packet (`0x01`) to `CTRL_UDP_PORT` on the PC. The Python receiver's `control_listener` thread picks this up and drives a `ListenState` state machine:

| State | Description |
|-------|-------------|
| `IDLE` | Waiting for wake word signal. Audio is streamed but not transcribed. |
| `SKIP_WAKEWORD_BLEED` | Discards `BLEED_SKIP_PACKETS` (~512 ms) of audio after the wake word — covers utterance bleed + begin chime duration. |
| `CAPTURING` | Actively recording the user's command. VAD accumulator builds a speech segment. |
| `TRANSCRIBING` | Groq STT (whisper-large-v3) is transcribing; new captures are blocked. |
| `RESPONDING` | LLM is generating a response / TTS is synthesizing speech; pipeline busy. |

Flow: `IDLE` → (wake packet received) → `SKIP_WAKEWORD_BLEED` → `CAPTURING` → (silence or max segment) → `TRANSCRIBING` → `RESPONDING` → (TTS playback ends) → `IDLE`.

When entering `CAPTURING`, the PC sends `0x02` to ESP32 to start a looping latency chime (fills the audio gap during STT/LLM). When TTS audio is ready, `0x03` stops the chime. On TTS failure or pipeline abort, `0x03` also resets the ESP32's audio state.

A `CAPTURE_TIMEOUT_S` (3 s) timer starts when entering `CAPTURING`; if no speech is detected by **Silero VAD** within that window, the state resets to `IDLE` (false-positive guard).

### ESP32 ↔ PC Contract

The ESP32 firmware (separate repo) sends UDP audio at 16 kHz, 512 uint16 samples per packet on port 12345.
Control packets flow on port 12346 (ESP32→PC: `0x01` = wake word) and port 12348 (PC→ESP32: `0x02` = start chime, `0x03` = stop chime).
TTS audio is sent back to the ESP32 on port 12347 as int16 PCM mono.

See the [Python config table below](#python-configuration-receiverpy-constants) for port/address constants.

### Python Backend

The receiver ([receiver.py](receiver.py)) is a multi-threaded design with 7 daemon threads:

- **`receive_loop`** — receives UDP datagrams, decodes samples (uint16 → float32, DC-offset removed), pushes to `packet_queue` (playback, noise-gated) and `vad_queue` (original audio for VAD). Drops oldest on overflow.
- **`control_listener`** — listens on `CTRL_PORT` for wake word trigger packets from the ESP32; drives the `ListenState` state machine. Includes a 1.5 s cooldown to debounce repeated triggers.
- **`vad_accumulator_loop`** — builds speech segments from `vad_queue` when in `CAPTURING` state using **Silero VAD** (PyTorch neural model, `SILERO_VAD_THRESHOLD=0.5`). Pushes completed segments to `transcribe_queue`. Includes false-positive timeout (`CAPTURE_TIMEOUT_S`). Sends `0x02` control byte to ESP32 on segment ready to start the latency chime.
- **`transcription_loop`** — calls **Groq STT** (`whisper-large-v3`) on completed segments with a `STT_TIMEOUT_S` (15 s) guard thread. Segments ≤3 words are discarded (wake-word bleed filter). Sends `0x02` to ESP32 to trigger chime loop during STT/LLM gap.
- **`llm_loop`** — receives transcripts from `transcription_loop` via `llm_queue`, streams a response from **OpenRouter** (DeepSeek V4 Flash) via the OpenAI SDK. Applies streaming sentence splitting (avoids splitting on abbreviations/initials), markdown stripping, and pushes each complete sentence to `tts_queue`. Maintains conversation history (capped at `CONVERSATION_HISTORY_MAX_TURNS=20`). Timeout safety: `LLM_TOKEN_TIMEOUT_S` (8 s between tokens) + `LLM_TOTAL_TIMEOUT_S` (45 s total).
- **`tts_loop`** — calls **OpenRouter TTS** (`hexgrad/kokoro-82m`, voice `af_bella`) on queued LLM sentences with a `TTS_TIMEOUT_S` (20 s) guard. Resamples TTS output from 24 kHz to 16 kHz via `scipy.signal.resample_poly`, then appends to `audio_queue` for the dispatch thread. Sends `0x03` to ESP32 to stop the latency chime on TTS failure.
- **`audio_dispatch_loop`** — drains `audio_queue` and routes completed TTS audio to the configured output(s): `"local"` (sounddevice callback), `"esp32"` (UDP to ESP32 on port 12347, paced to real-time with deadline-based timing), or `"both"`. Returns to `IDLE` state after sending.
- **`audio_callback`** — `sounddevice` callback. In normal mode drains `packet_queue` (mic passthrough). When `is_responding=True`, drains `response_queue` (TTS audio from LLM). Handles leftover-sample carry between callbacks. A `None` sentinel in the queue signals end of TTS playback.

Requires two environment variables: `GROQ_API_KEY` and `OPENROUTER_API_KEY`.

On startup, a background warmup thread primes the DeepSeek KV cache with a dummy request to reduce first-interaction latency.

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
| `ESP32_IP` | — | ESP32's IP address for TTS audio UDP |
| `GROQ_MODEL` | `"whisper-large-v3"` | Groq STT model for transcription |
| `TTS_MODEL` | `"hexgrad/kokoro-82m"` | OpenRouter TTS model |
| `TTS_VOICE` | `"af_bella"` | TTS voice name |
| `TTS_PCM_RATE` | `24000` | TTS API output sample rate (resampled to 16 kHz) |
| `LLM_MODEL` | `"deepseek/deepseek-v4-flash"` | OpenRouter model for LLM responses |
| `VAD_SILENCE_MS` | `500` | Trailing silence required to end a speech segment |
| `VAD_MIN_SPEECH_MS` | `400` | Minimum speech length; shorter segments are discarded |
| `MAX_SEGMENT_S` | `10` | Hard cap — force transcribe even if no silence detected |
| `CAPTURE_TIMEOUT_S` | `3` | Seconds of silence after wake word before treating as false positive |
| `CONVERSATION_HISTORY_MAX_TURNS` | `20` | Max message objects in LLM history (~10 exchanges) |
| `CTRL_PORT` | `12346` | UDP port for wake word trigger signal (must match firmware `CTRL_UDP_PORT`) |
| `BLEED_SKIP_PACKETS` | `16` | Packets to discard after wake word (~512 ms: covers utterance bleed + begin chime) |
| `STT_TIMEOUT_S` | `15` | Max seconds to wait for Groq STT response |
| `LLM_TOKEN_TIMEOUT_S` | `8` | Max seconds between tokens in LLM stream |
| `LLM_TOTAL_TIMEOUT_S` | `45` | Hard cap on total LLM response time |
| `TTS_TIMEOUT_S` | `20` | Max seconds to wait for TTS response |

#### Running the receiver

Requires two API keys set as environment variables. Also requires `ESP32_IP` in `receiver.py` to match the ESP32's printed IP address:

```bash
# Required: set your API keys before running
export GROQ_API_KEY="gsk_..."
export OPENROUTER_API_KEY="sk-or-..."

# Check receiver.py and set ESP32_IP to the address printed by the ESP32 on boot

# Install dependencies (requires uv)
uv sync

# Run
uv run receiver.py
```

On Windows (PowerShell):
```powershell
$env:GROQ_API_KEY="gsk_..."
$env:OPENROUTER_API_KEY="sk-or-..."
uv run receiver.py
```

The `AUDIO_OUTPUT` constant in `receiver.py` controls audio routing: `"esp32"` (default — sends TTS to ESP32 for I2S playback), `"local"` (plays on the PC running receiver.py), or `"both"`.

ESP32 streams live diagnostic counts to serial: `Sent: N | Failed: N`. A rising `Failed` count indicates network or send-path issues.

## Troubleshooting

### Python receiver stays at "Waiting for N packets to pre-buffer..."
1. Verify ESP32 and PC are on the same subnet (ESP32 streams to `PC_IP`, not broadcast)
2. Check firewall allows UDP port 12345 inbound
3. Confirm `PC_IP` in the ESP32 firmware matches the machine running `receiver.py`
4. ESP32 firmware shows `Sent:` and `Failed:` counters on serial — `Failed` incrementing indicates send errors

### Receiver boots but no audio / errors
- Confirm both `GROQ_API_KEY` and `OPENROUTER_API_KEY` are set before running
- Verify `ESP32_IP` in [receiver.py](receiver.py) matches the ESP32's boot-printed IP
- Check `AUDIO_OUTPUT` is set to `"local"` if no ESP32 is available for testing
- Set `RECORDING_MODE = True` to test mic-to-speaker passthrough without any API calls
