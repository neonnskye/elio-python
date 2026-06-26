"""
music_detector_rpi.py
----------------------
Detects whether music is playing using continuous microphone input,
YAMNet via tflite-runtime (no MediaPipe), and PyAudio.

When music is detected, opens a SECOND independent audio stream just
for recording, so the detection loop is never blocked or starved.

File structure expected:
    music_detector_rpi.py   ← this file
    acrcloud.py             ← your existing ACRCloud script (unchanged)
    models/
        yamnet.tflite
        yamnet_class_map.csv

Setup:
    pip install tflite-runtime pyaudio numpy requests scipy

Environment variables required:
    set ACR_ACCESS_KEY=your_key          # Windows
    set ACR_ACCESS_SECRET=your_secret

    export ACR_ACCESS_KEY=your_key       # Linux / Pi
    export ACR_ACCESS_SECRET=your_secret
"""

import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave

import numpy as np
import pyaudio
from scipy.signal import resample_poly

try:
    import tflite_runtime.interpreter as tflite  # Raspberry Pi
except ModuleNotFoundError:
    import tensorflow as tf  # Windows / Mac

    tflite = tf.lite

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL_PATH = "models/yamnet.tflite"
CLASS_MAP_CSV = "models/yamnet_class_map.csv"

SAMPLE_RATE = 16_000
CHANNELS = 1
CHUNK_DURATION_S = 0.975
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_DURATION_S)  # ~15 600 samples

MUSIC_LABELS = {
    "music",
    "musical instrument",
    "singing",
    "song",
    "guitar",
    "piano",
    "drum",
    "bass",
    "violin",
    "orchestra",
    "choir",
    "beat",
    "rhythm",
    "pop music",
    "rock music",
    "hip hop",
    "jazz",
    "electronic music",
    "classical music",
    "dance music",
}

CONFIDENCE_THRESHOLD = 0.15
SMOOTHING_WINDOW = 4

# ACRCloud settings
RECORD_SECONDS = 7  # seconds to record for fingerprinting
ACR_SAMPLE_RATE = 44100  # ACRCloud works best at 44100 Hz

# Grace period: music must be absent for this long before we consider it
# truly "off". Prevents a brief dip (breath, quiet bar, etc.) from
# resetting the trigger during a continuous track.
MUSIC_OFF_GRACE_S = 5.0

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

recent_music_flags: list[bool] = []
state_lock = threading.Lock()

# Rising-edge detection state
music_was_confirmed_off: bool = True  # True at startup so first music fires
music_off_since: float | None = None  # wall-clock time music last dropped
identifying: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_music_label(label_name: str) -> bool:
    lower = label_name.lower()
    return any(keyword in lower for keyword in MUSIC_LABELS)


def load_class_map(csv_path: str) -> list[str]:
    labels = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            labels.append(row["display_name"])
    return labels


def build_interpreter() -> tflite.Interpreter:
    interpreter = tflite.Interpreter(model_path=MODEL_PATH)
    interpreter.allocate_tensors()
    return interpreter


def classify_chunk(
    interpreter: tflite.Interpreter,
    pcm: np.ndarray,
    labels: list[str],
) -> list[tuple[str, float]]:
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()

    input_tensor = pcm.reshape(-1).astype(np.float32)
    interpreter.set_tensor(input_details[0]["index"], input_tensor)
    interpreter.invoke()

    scores = interpreter.get_tensor(output_details[0]["index"])
    mean_scores = scores.mean(axis=0)

    results = [
        (labels[i], float(mean_scores[i]))
        for i in range(len(mean_scores))
        if float(mean_scores[i]) >= CONFIDENCE_THRESHOLD
    ]
    results.sort(key=lambda x: x[1], reverse=True)
    return results


# ---------------------------------------------------------------------------
# ACRCloud: open own stream → record → resample → WAV → subprocess
# ---------------------------------------------------------------------------


def record_and_identify(pa: pyaudio.PyAudio) -> None:
    """
    Opens its OWN independent PyAudio input stream for recording.
    This means the detection loop stream is NEVER touched or blocked.

    Steps:
        1. Open a fresh mic stream at 16 kHz (same device, different handle)
        2. Record RECORD_SECONDS of audio
        3. Close the recording stream immediately
        4. Resample 16 kHz → 44100 Hz  (ACRCloud fingerprint quality)
        5. Save as a temp WAV
        6. Call acrcloud.py via subprocess (file completely unchanged)
        7. Pretty-print the song result
    """
    global identifying

    print(f"\n\n🎙️  Recording {RECORD_SECONDS}s on separate stream …", flush=True)

    # ── 1. Open a dedicated recording stream ─────────────────────────────
    #   Using a small 1024-sample buffer keeps latency low and avoids
    #   the overflow that plagued the shared-stream approach.
    rec_chunk = 1024
    try:
        rec_stream = pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=rec_chunk,
        )
    except Exception as e:
        print(f"⚠️  Could not open recording stream: {e}")
        identifying = False
        return

    # ── 2. Record ─────────────────────────────────────────────────────────
    frames = []
    total_samples = RECORD_SECONDS * SAMPLE_RATE
    recorded = 0

    while recorded < total_samples:
        needed = min(rec_chunk, total_samples - recorded)
        raw = rec_stream.read(needed, exception_on_overflow=False)
        frames.append(raw)
        recorded += needed

    rec_stream.stop_stream()
    rec_stream.close()
    print("✅  Recording done.", flush=True)

    # ── 3. Resample 16000 Hz → 44100 Hz ──────────────────────────────────
    #   resample_poly(data, up=441, down=160):
    #       16000 × (441 / 160) = 44100  (exact integer ratio, no drift)
    pcm_int16 = np.frombuffer(b"".join(frames), dtype=np.int16)
    pcm_float = pcm_int16.astype(np.float32)
    resampled_float = resample_poly(pcm_float, up=441, down=160)
    resampled_int16 = np.clip(resampled_float, -32768, 32767).astype(np.int16)

    # ── 4. Save WAV ───────────────────────────────────────────────────────
    tmp_path = tempfile.mktemp(suffix=".wav")
    with wave.open(tmp_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(ACR_SAMPLE_RATE)  # 44100 Hz
        wf.writeframes(resampled_int16.tobytes())

    print(f"💾  WAV saved ({ACR_SAMPLE_RATE} Hz) → {tmp_path}", flush=True)

    # ── 5. Call acrcloud.py as subprocess ────────────────────────────────
    acrcloud_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "acrcloud.py"
    )
    print("🔍  Sending to ACRCloud …", flush=True)

    try:
        result = subprocess.run(
            [sys.executable, acrcloud_script, tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        raw_output = result.stdout or result.stderr or "(no output)"

        # ── 6. Pretty-print result ─────────────────────────────────────
        print("\n" + "─" * 60)
        try:
            data = json.loads(raw_output)
            code = data.get("status", {}).get("code")

            if code == 0:
                music = data["metadata"]["music"][0]
                title = music.get("title", "Unknown")
                artists = ", ".join(a["name"] for a in music.get("artists", []))
                album = music.get("album", {}).get("name", "Unknown")
                print("✅  Song identified!")
                print(f"    🎵 Title  : {title}")
                print(f"    👤 Artist : {artists}")
                print(f"    💿 Album  : {album}")
            elif code == 1001:
                print("❌  No match (not in ACRCloud database, or audio too quiet)")
            else:
                msg = data.get("status", {}).get("msg", "")
                print(f"⚠️  ACRCloud code {code}: {msg}")
        except (json.JSONDecodeError, KeyError):
            print(raw_output)
        print("─" * 60 + "\n")

    except subprocess.TimeoutExpired:
        print("⚠️  ACRCloud request timed out.")
    except FileNotFoundError:
        print(f"⚠️  acrcloud.py not found at: {acrcloud_script}")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        identifying = False


# ---------------------------------------------------------------------------
# Process YAMNet results + trigger identification
# ---------------------------------------------------------------------------


def process_results(
    results: list[tuple[str, float]],
    timestamp_ms: int,
    pa: pyaudio.PyAudio,  # passed so record thread can open its own stream
) -> None:
    global music_was_confirmed_off, music_off_since, identifying

    music_detected = any(is_music_label(name) for name, _ in results)
    music_hits = [(n, s) for n, s in results if is_music_label(n)]

    with state_lock:
        recent_music_flags.append(music_detected)
        if len(recent_music_flags) > SMOOTHING_WINDOW:
            recent_music_flags.pop(0)
        smoothed = (
            sum(recent_music_flags) / len(recent_music_flags)
            if recent_music_flags
            else 0.0
        )
        music_on = smoothed >= 0.5

    now = time.time()

    # ── Grace-period tracking ────────────────────────────────────────────
    # When music goes off, start a timer. Only mark it "confirmed off"
    # once it has been gone for the full grace period. If music comes back
    # before the timer expires, cancel it — it never really went away.
    if not music_on:
        if music_off_since is None:
            music_off_since = now  # start the off-timer
        elif (
            now - music_off_since
        ) >= MUSIC_OFF_GRACE_S and not music_was_confirmed_off:
            music_was_confirmed_off = True
            print("\n🔕  Music confirmed off (grace period elapsed).", flush=True)
    else:
        music_off_since = None  # music is back — reset off-timer

    # ── Rising-edge trigger ──────────────────────────────────────────────
    # Fire only when music comes ON after a confirmed-off period.
    if music_on and music_was_confirmed_off and not identifying:
        music_was_confirmed_off = False  # consume the rising edge
        identifying = True
        t = threading.Thread(
            target=record_and_identify,
            args=(pa,),
            daemon=True,
        )
        t.start()

    # ── Status line ──────────────────────────────────────────────────────
    grace_indicator = ""
    if not music_on and music_off_since is not None and not music_was_confirmed_off:
        elapsed = now - music_off_since
        remaining = max(0.0, MUSIC_OFF_GRACE_S - elapsed)
        grace_indicator = f"  [grace {remaining:.1f}s]"

    status = "🎵 MUSIC" if music_on else "🔇 No music"
    if music_hits:
        top_label, top_score = music_hits[0]
        print(
            f"\r[{timestamp_ms:>8} ms] {status}  ({top_label}: {top_score:.2f}){grace_indicator}        ",
            end="",
            flush=True,
        )
    else:
        print(
            f"\r[{timestamp_ms:>8} ms] {status}{grace_indicator}                                        ",
            end="",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("Loading YAMNet class map …")
    labels = load_class_map(CLASS_MAP_CSV)

    print("Loading YAMNet model …")
    interpreter = build_interpreter()
    print("Model loaded. Listening … (Ctrl-C to stop)\n")

    pa = pyaudio.PyAudio()

    # Detection stream — runs continuously, never touched by the record thread
    detect_stream = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SAMPLES,
    )

    timestamp_ms = 0
    chunk_duration_ms = int(CHUNK_DURATION_S * 1000)

    try:
        while True:
            raw = detect_stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
            pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

            results = classify_chunk(interpreter, pcm, labels)
            process_results(results, timestamp_ms, pa)  # pa, not stream

            timestamp_ms += chunk_duration_ms

    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        detect_stream.stop_stream()
        detect_stream.close()
        pa.terminate()


if __name__ == "__main__":
    main()
