"""
music_detector.py
-----------------
Detects whether music is playing using continuous microphone input,
YAMNet via MediaPipe's AudioClassifier, and PyAudio.

Setup:
    pip install mediapipe pyaudio numpy
    wget -O yamnet.tflite \
        https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite
"""

import threading

import numpy as np
import pyaudio
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import audio as mp_audio
from mediapipe.tasks.python.components.containers import AudioData

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL_PATH = "yamnet.tflite"  # path to the downloaded .tflite model

SAMPLE_RATE = 16_000  # YAMNet expects 16 kHz
CHANNELS = 1  # mono
CHUNK_DURATION_S = 0.975  # YAMNet's native window size (seconds)
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_DURATION_S)  # ~15 600 samples

# YAMNet label substrings that count as "music".
# The full label list is at: https://github.com/tensorflow/models/blob/master/research/audioset/yamnet/yamnet_class_map.csv
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

CONFIDENCE_THRESHOLD = 0.15  # minimum score to consider a label active
SMOOTHING_WINDOW = 4  # number of consecutive results to smooth over

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

recent_music_flags: list[bool] = []
state_lock = threading.Lock()


def is_music_label(label_name: str) -> bool:
    """Return True if the label name contains any music-related keyword."""
    lower = label_name.lower()
    return any(keyword in lower for keyword in MUSIC_LABELS)


def on_result(result, timestamp_ms: int):
    """
    Callback invoked by MediaPipe after each audio chunk is classified.
    Runs on a MediaPipe internal thread.
    """
    music_detected = False

    if result and result.classifications:
        for classification in result.classifications:
            for category in classification.categories:
                if category.score >= CONFIDENCE_THRESHOLD and is_music_label(
                    category.category_name
                ):
                    music_detected = True
                    top_label = category.category_name
                    top_score = category.score
                    break
            if music_detected:
                break

    with state_lock:
        recent_music_flags.append(music_detected)
        # Keep only the last SMOOTHING_WINDOW results
        if len(recent_music_flags) > SMOOTHING_WINDOW:
            recent_music_flags.pop(0)

        smoothed = (
            sum(recent_music_flags) / len(recent_music_flags)
            if recent_music_flags
            else 0.0
        )
        music_on = smoothed >= 0.5

    status = "🎵 MUSIC" if music_on else "🔇 No music"
    if music_detected:
        print(
            f"\r[{timestamp_ms:>8} ms] {status}  ({top_label}: {top_score:.2f})        ",
            end="",
            flush=True,
        )
    else:
        print(
            f"\r[{timestamp_ms:>8} ms] {status}                                        ",
            end="",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Build the MediaPipe AudioClassifier (AUDIO_STREAM mode)
# ---------------------------------------------------------------------------


def build_classifier() -> mp_audio.AudioClassifier:
    base_options = mp_python.BaseOptions(model_asset_path=MODEL_PATH)
    options = mp_audio.AudioClassifierOptions(
        base_options=base_options,
        running_mode=mp_audio.RunningMode.AUDIO_STREAM,
        max_results=5,
        score_threshold=CONFIDENCE_THRESHOLD,
        result_callback=on_result,
    )
    return mp_audio.AudioClassifier.create_from_options(options)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    print("Loading YAMNet model …")
    classifier = build_classifier()
    print("Model loaded. Listening … (Ctrl-C to stop)\n")

    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SAMPLES,
    )

    # Timestamp counter in milliseconds — must be strictly increasing.
    timestamp_ms = 0
    chunk_duration_ms = int(CHUNK_DURATION_S * 1000)

    try:
        while True:
            raw = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)

            # Convert int16 PCM → float32 in [-1, 1]
            pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

            # Wrap in MediaPipe's AudioData container
            audio_data = AudioData.create_from_array(
                pcm,
                sample_rate=SAMPLE_RATE,
            )

            # Fire-and-forget — result arrives in on_result() callback
            classifier.classify_async(audio_data, timestamp_ms)
            timestamp_ms += chunk_duration_ms

    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()
        classifier.close()


if __name__ == "__main__":
    main()
