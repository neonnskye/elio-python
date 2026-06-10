import collections
import concurrent.futures
import io
import json
import os
import platform
import queue
import random
import re
import socket
import sys
import threading
import time
import wave
from datetime import datetime
from enum import Enum, auto
from math import gcd
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
import scipy.signal
import sounddevice as sd
import torch
from groq import Groq
from openai import OpenAI
from piper import PiperVoice

# Sentinel pushed onto tts_queue after all sentences from one LLM turn are queued.
# tts_dispatcher_loop forwards it to audio_queue (via audio_collector_loop) as None,
# which audio_dispatch_loop uses to call reset_to_idle exactly once per turn.
TTS_TURN_DONE = object()


def ts() -> str:
    """Return a human-readable timestamp string."""
    return datetime.now().strftime("[%H:%M:%S.%f")[:-3] + "]"


class ListenState(Enum):
    IDLE = auto()  # waiting for wake word signal
    SKIP_WAKEWORD_BLEED = (
        auto()
    )  # discarding audio bleed from the wake word utterance itself
    CAPTURING = auto()  # actively recording the user's command
    TRANSCRIBING = auto()  # Whisper is processing, block new captures
    RESPONDING = auto()  # LLM is generating a response


# ---- Configuration ----
UDP_IP = "0.0.0.0"  # Listen on all interfaces
UDP_PORT = 12345
SAMPLE_RATE = 16000
SAMPLES_PER_PKT = 512
PREBUFFER_PKTS = 3  # Packets to queue before starting playback (~96ms)
MAX_QUEUE_LEN = 10  # Drop oldest if queue grows beyond this (~320ms)
NOISE_GATE = 0  # RMS threshold below which a packet is muted (0 = off)
SILERO_VAD_THRESHOLD = 0.5  # Silero VAD speech probability threshold
# -----------------------

# ---- Recording Mode ----
# Set True to capture mic passthrough audio for wake word dataset collection.
# Disables wake word handling, VAD, STT, LLM, TTS, and all API calls.
# Only mic → speaker passthrough remains active.
RECORDING_MODE = False
# ------------------------

# Audio output routing
AUDIO_OUTPUT = "esp32"  # "local" | "esp32" | "both"
ESP32_MDNS_HOST = "esp32-audio"  # mDNS hostname broadcast by the ESP32
ESP32_IP = ""  # resolved via mDNS at startup
ESP32_AUDIO_PORT = 12347
AUDIO_SEND_CHUNK = 512  # samples per UDP packet
AUDIO_SEND_RATE = 16000  # Hz
AUDIO_SEND_SLEEP = AUDIO_SEND_CHUNK / AUDIO_SEND_RATE  # 0.032s — real-time pacing

# Groq STT config
GROQ_MODEL = "whisper-large-v3"

# TTS config (Piper local)
PIPER_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "en_US-hfc_female-medium.onnx"
)
TTS_PCM_RATE = 22050  # Piper medium-quality voices output at 22050 Hz

# LLM config
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
LLM_MODEL = "deepseek-v4-flash"


def _load_system_prompt() -> str:
    """Read the LLM system prompt from system.md (sibling of receiver.py)."""
    prompt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system.md")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read().strip()


LLM_SYSTEM_PROMPT = _load_system_prompt()

STORIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stories.json")

# Keywords that trigger the story-picker (case-insensitive)
STORY_KEYWORDS = {"story", "stories", "tale", "tales"}


def pick_random_story() -> dict | None:
    """Pick a random story from stories.json.

    Returns a dict with 'number', 'title', and 'description', or None if
    the file is missing or malformed.
    """
    try:
        with open(STORIES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        stories_list = data["stories"]
        target_number = random.randint(1, len(stories_list))
        for story in stories_list:
            if story["number"] == target_number:
                return story
    except Exception as exc:
        print(f"{ts()} [stories] Could not load stories.json: {exc}", flush=True)
    return None


def augment_story_prompt(transcript: str) -> str:
    """If the transcript contains a story keyword, pick a random story and
    inject a SYSTEM NOTE into the prompt so the LLM narrates that specific
    story instead of making one up.

    Returns the (possibly augmented) transcript string.
    """
    words = set(re.findall(r"[a-z]+", transcript.lower()))
    if not words & STORY_KEYWORDS:
        return transcript  # no story keyword — pass through unchanged

    story = pick_random_story()
    if story is None:
        return transcript  # couldn't load stories, fall back to default behaviour

    note = (
        f" [SYSTEM NOTE: The system has randomly selected the following story for "
        f'this request. Please narrate it: "{story["title"]}" — {story["description"]}]'
    )
    print(
        f"{ts()} [stories] Story keyword detected. Selected #{story['number']}: "
        f"{story['title']}",
        flush=True,
    )
    return transcript + note


# VAD / segmentation config
VAD_SILENCE_MS = 500  # ms of silence before we consider speech done
VAD_MIN_SPEECH_MS = 400  # ignore speech segments shorter than this
MAX_SEGMENT_S = 10  # hard cap — transcribe even if no silence detected
CAPTURE_TIMEOUT_S = (
    3  # seconds of silence after wake word before treating as false positive
)
# -----------------------

# Timeout safety config
STT_TIMEOUT_S = 15  # max seconds to wait for Groq STT response
LLM_TOKEN_TIMEOUT_S = 8  # max seconds between tokens in LLM stream
LLM_TOTAL_TIMEOUT_S = 45  # hard cap on total LLM response time
TTS_TIMEOUT_S = 20  # max seconds to wait for TTS response
CONVERSATION_HISTORY_MAX_TURNS = (
    20  # max message objects in history (20 = ~10 exchanges)
)

# Wake word gating
MQTT_BROKER = "127.0.0.1"  # broker runs locally
MQTT_PORT = 1883
TOPIC_WAKE = "elio/wake"
TOPIC_CTRL = "elio/ctrl"
TOPIC_STATE = "elio/state"
TOPIC_TRANSCRIPT_USER = "elio/transcript/user"
TOPIC_TRANSCRIPT_ASST = "elio/transcript/assistant"
TOPIC_SYSTEM_READY = "elio/system/ready"
TOPIC_SYSTEM_SHUTDOWN = "elio/system/shutdown"

BLEED_SKIP_PACKETS = (
    16  # ~768ms: covers "elio" utterance bleed (~256ms) + begin chime (~512ms)
)


def resolve_mdns(hostname: str, timeout: int = 15) -> str:
    """Resolve a .local mDNS hostname to an IP address.

    Uses zeroconf directly to bypass the OS resolver (avahi NSS / Bonjour),
    which can block for 3-8 seconds on a cold cache. The zeroconf path sends
    a raw multicast A-record query and polls the response cache at 50ms
    intervals, typically resolving in under 500ms.

    Falls back to socket.getaddrinfo() (with a 3s per-attempt thread timeout)
    if zeroconf is unavailable.
    """
    fqdn = hostname if hostname.endswith(".local") else f"{hostname}.local"
    # zeroconf cache keys always include the trailing dot
    fqdn_dot = fqdn if fqdn.endswith(".") else fqdn + "."
    print(f"{ts()} Resolving {fqdn} via mDNS...", flush=True)
    t0 = time.monotonic()

    # --- Fast path: direct zeroconf A-record query ---
    # Stable import locations for modern zeroconf (0.32+):
    #   - _TYPE_A / _CLASS_IN  -> zeroconf.const
    #   - DNSOutgoing          -> zeroconf._protocol.outgoing
    #   - DNSAddress / DNSQuestion -> zeroconf._dns
    # Older versions exported these from the top-level package; those were
    # removed, hence the ImportError you saw.
    try:
        from zeroconf import Zeroconf
        from zeroconf._dns import DNSAddress, DNSQuestion
        from zeroconf._protocol.outgoing import DNSOutgoing
        from zeroconf.const import _CLASS_IN, _TYPE_A

        zc = Zeroconf()
        deadline = time.monotonic() + timeout
        try:
            # Fire an mDNS query; the ESP32's response will populate zc.cache
            out = DNSOutgoing(0x0000)  # FLAGS_QR_QUERY = 0
            out.add_question(DNSQuestion(fqdn_dot, _TYPE_A, _CLASS_IN))
            zc.send(out)

            while time.monotonic() < deadline:
                record = zc.cache.get_by_details(fqdn_dot, _TYPE_A, _CLASS_IN)
                if record and isinstance(record, DNSAddress):
                    ip = socket.inet_ntoa(record.address)
                    elapsed = time.monotonic() - t0
                    print(f"{ts()} Resolved {fqdn} -> {ip} (zeroconf, {elapsed:.2f}s)")
                    return ip
                time.sleep(0.05)
        finally:
            zc.close()
        print(f"{ts()} zeroconf timed out, falling back to OS resolver...", flush=True)
    except Exception as exc:
        print(
            f"{ts()} zeroconf unavailable ({exc}), falling back to OS resolver...",
            flush=True,
        )

    # --- Fallback: OS resolver ---
    # socket.getaddrinfo() can block for several seconds per call on a cold
    # avahi/Bonjour cache, so we run each attempt in a daemon thread with a
    # hard 3s timeout to prevent a single hung call from eating all the time.
    def _try_getaddrinfo(out: list) -> None:
        try:
            out.append(socket.getaddrinfo(fqdn, None)[0][4][0])
        except socket.gaierror:
            pass

    for attempt in range(timeout):
        result: list = []
        t = threading.Thread(target=_try_getaddrinfo, args=(result,), daemon=True)
        t.start()
        t.join(timeout=3.0)
        if result:
            elapsed = time.monotonic() - t0
            print(
                f"{ts()} Resolved {fqdn} -> {result[0]} (OS resolver, {elapsed:.2f}s)"
            )
            return result[0]
        print(f"  attempt {attempt + 1}/{timeout} failed, retrying...")

    raise RuntimeError(
        f"mDNS resolution failed for {fqdn} after {timeout}s. "
        "Ensure avahi-daemon is running on the Pi, or Bonjour is running on Windows."
    )


def start_windows_mdns_broadcast(service_name: str = "raspberrypi") -> None:
    """
    On Windows, broadcast this machine as `<service_name>.local` via zeroconf.
    Not needed on Linux/macOS where avahi/mDNS handles this at the OS level.
    """
    if platform.system() != "Windows":
        return
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        print(
            "WARNING: zeroconf package not installed. "
            "Run `pip install zeroconf` to enable mDNS broadcast on Windows."
        )
        return

    local_ip = socket.gethostbyname(socket.gethostname())
    info = ServiceInfo(
        "_http._tcp.local.",
        f"{service_name}._http._tcp.local.",
        addresses=[socket.inet_aton(local_ip)],
        port=80,
        properties={},
        server=f"{service_name}.local.",
    )
    zc = Zeroconf()
    zc.register_service(info)
    print(
        f"{ts()} [Windows] Broadcasting this machine as '{service_name}.local' ({local_ip}) via zeroconf"
    )
    # zc intentionally not unregistered — runs for the lifetime of the script


listen_state = ListenState.IDLE
bleed_remaining = 0
state_lock = threading.Lock()

packet_queue: collections.deque = collections.deque()
vad_queue: collections.deque = collections.deque()
llm_queue: queue.Queue = queue.Queue()
queue_lock = threading.Lock()
leftover: np.ndarray = np.zeros(0, dtype=np.float32)
response_queue: collections.deque = collections.deque()
is_responding: bool = False

# TTS queue for LLM responses to be spoken aloud (text sentences)
tts_queue: queue.Queue = queue.Queue()

# Queue for completed TTS audio (decouples synthesis from playback dispatch)
audio_queue: collections.deque = collections.deque()
audio_queue_lock = threading.Lock()
audio_queue_event = threading.Event()

# A separate queue to pass completed audio segments to the transcription thread
transcribe_queue: queue.Queue = queue.Queue()

# Shutdown coordination
shutdown_event = threading.Event()

# Conversation history for LLM context across voice turns
conversation_history: list[dict] = []
history_lock = threading.Lock()

# UDP socket for sending TTS audio to ESP32
audio_send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

mqtt_client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="elio-receiver"
)

# --- VAD accumulator state ---
accumulator: list[np.ndarray] = []
silence_packets = 0
SILENCE_PACKETS_MAX = int((VAD_SILENCE_MS / 1000) * SAMPLE_RATE / SAMPLES_PER_PKT)
MIN_SPEECH_PACKETS = int((VAD_MIN_SPEECH_MS / 1000) * SAMPLE_RATE / SAMPLES_PER_PKT)
MAX_SEGMENT_PACKETS = int(MAX_SEGMENT_S * SAMPLE_RATE / SAMPLES_PER_PKT)

vad_model = None


def load_silero_vad() -> None:
    """Load the Silero VAD model from the local models directory."""
    global vad_model
    print(f"{ts()} Loading Silero VAD model...", flush=True)

    # Define the local path to your VAD model directory
    local_vad_path = Path(__file__).parent / "models" / "silero-vad"

    if not local_vad_path.exists():
        raise FileNotFoundError(f"VAD model directory not found at {local_vad_path}")

    # Load using source="local"
    model, _ = torch.hub.load(
        repo_or_dir=str(local_vad_path),
        model="silero_vad",
        source="local",  # Tells PyTorch to look at the local disk, not GitHub
        trust_repo=True,
    )

    model.eval()
    vad_model = model
    print(f"{ts()} Silero VAD model loaded from local directory.", flush=True)


_last_wake_time: float = 0.0
_WAKE_COOLDOWN_S: float = 1.5


def on_mqtt_message(client, userdata, msg) -> None:
    """MQTT callback — fires when a message arrives on any subscribed topic.
    Replaces control_listener(). Currently handles elio/wake only.
    """
    global listen_state, bleed_remaining, _last_wake_time

    if msg.topic != TOPIC_WAKE:
        return

    if RECORDING_MODE:
        return

    now = time.monotonic()
    if now - _last_wake_time < _WAKE_COOLDOWN_S:
        return
    _last_wake_time = now

    with state_lock:
        if listen_state != ListenState.IDLE:
            print(
                f"{ts()} [CTRL] Wake signal received but state is "
                f"{listen_state.name}, ignoring."
            )
            return
        listen_state = ListenState.SKIP_WAKEWORD_BLEED
        bleed_remaining = BLEED_SKIP_PACKETS

    mqtt_publish(TOPIC_STATE, "listening")

    print(
        f"\n{ts()} [WAKE] Wake word received via MQTT! "
        f"Skipping {BLEED_SKIP_PACKETS} packets of bleed..."
    )


def vad_accumulator_loop() -> None:
    global accumulator, silence_packets, listen_state, bleed_remaining

    if RECORDING_MODE:
        print(f"{ts()} [RECORDING MODE] vad_accumulator_loop disabled.")
        return

    capture_start = 0.0  # timestamp when CAPTURING began

    while not shutdown_event.is_set():
        chunk = None
        with queue_lock:
            if vad_queue:
                chunk = vad_queue.popleft()

        if chunk is None:
            try:
                shutdown_event.wait(0.005)
            except KeyboardInterrupt:
                pass
            continue

        with state_lock:
            current_state = listen_state

        # --- IDLE: discard everything ---
        if current_state == ListenState.IDLE:
            continue

        # --- SKIP_WAKEWORD_BLEED: count down and discard ---
        if current_state == ListenState.SKIP_WAKEWORD_BLEED:
            with state_lock:
                bleed_remaining -= 1
                if bleed_remaining <= 0:
                    listen_state = ListenState.CAPTURING
                    accumulator = []
                    silence_packets = 0
                    capture_start = time.monotonic()
                    vad_model.reset_states()  # reset internal LSTM state for new session
                    print(f"{ts()} [WAKE] Bleed skip done. Capturing command now...")
            continue

        # --- TRANSCRIBING / RESPONDING: don't accumulate while busy ---
        if current_state in (ListenState.TRANSCRIBING, ListenState.RESPONDING):
            continue

        # --- CAPTURING: Silero VAD logic ---
        audio_tensor = torch.from_numpy(chunk).float().unsqueeze(0)  # (1, 512)

        with torch.no_grad():
            speech_prob = vad_model(audio_tensor, SAMPLE_RATE).item()

        is_speech = speech_prob >= SILERO_VAD_THRESHOLD

        sys.stdout.write(
            f"\rVAD prob={speech_prob:.3f} speech={is_speech} acc_len={len(accumulator)} "
        )
        sys.stdout.flush()

        # Capture timeout: if no speech has started within CAPTURE_TIMEOUT_S, false positive
        if not accumulator and not is_speech:
            if time.monotonic() - capture_start >= CAPTURE_TIMEOUT_S:
                print(
                    f"\n{ts()} [VAD] No speech detected for {CAPTURE_TIMEOUT_S}s — false positive, resetting to IDLE"
                )
                reset_to_idle("no speech after wake word")
                continue

        if is_speech:
            accumulator.append(chunk)
            silence_packets = 0
        else:
            if accumulator:
                silence_packets += 1
                accumulator.append(chunk)

        if accumulator:
            end_of_speech = (not is_speech) and (silence_packets >= SILENCE_PACKETS_MAX)
            too_long = len(accumulator) >= MAX_SEGMENT_PACKETS

            if end_of_speech or too_long:
                segment = np.concatenate(accumulator)
                if len(accumulator) >= MIN_SPEECH_PACKETS:
                    print(
                        f"\n{ts()} [VAD] Segment ready: {len(accumulator)} packets, {len(segment)} samples"
                    )
                    with state_lock:
                        listen_state = ListenState.TRANSCRIBING
                    mqtt_publish(TOPIC_STATE, "transcribing")
                    transcribe_queue.put(segment)
                    # Turn off the listen LED — user has finished speaking.
                    # 0x03 doubles as chime-stop; at this point the chime loop
                    # hasn't started yet (0x02 fires after), so on the ESP32 side
                    # this only has the LED effect.
                    mqtt_send_ctrl("stop")
                    mqtt_send_ctrl("processing")
                else:
                    print(
                        f"\n{ts()} [VAD] Segment too short ({len(accumulator)} pkts), discarding"
                    )
                    reset_to_idle("speech segment too short")
                accumulator = []
                silence_packets = 0
                vad_model.reset_states()  # reset internal LSTM state — session done


def segment_to_wav(segment: np.ndarray) -> bytes:
    """Convert a float32 numpy audio array to an in-memory WAV file."""
    # Clamp to [-1.0, 1.0] and convert to int16
    clipped = np.clip(segment, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    buf.seek(0)
    return buf


def transcription_loop() -> None:
    global listen_state

    client = Groq()  # uses GROQ_API_KEY env var

    while not shutdown_event.is_set():
        try:
            segment: np.ndarray = transcribe_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if segment is None:
            break

        result_holder = {}  # shared dict to get return value out of thread

        def do_transcribe():
            try:
                wav_buf = segment_to_wav(segment)
                transcription = client.audio.transcriptions.create(
                    file=("segment.wav", wav_buf),
                    model=GROQ_MODEL,
                    language="en",
                    response_format="text",
                    temperature=0.0,
                )
                result_holder["text"] = (
                    transcription.text
                    if hasattr(transcription, "text")
                    else str(transcription).strip()
                )
            except Exception as exc:
                result_holder["error"] = exc

        print(
            f"{ts()} [transcribe] Got segment of {len(segment)} samples, transcribing via Groq...",
            flush=True,
        )

        t0 = time.monotonic()
        t = threading.Thread(target=do_transcribe, daemon=True)
        t.start()
        t.join(timeout=STT_TIMEOUT_S)
        stt_elapsed = time.monotonic() - t0

        if t.is_alive():
            print(
                f"{ts()} [transcribe] TIMEOUT after {STT_TIMEOUT_S}s — resetting to IDLE",
                flush=True,
            )
            reset_to_idle("STT timeout")
            continue

        if "error" in result_holder:
            print(f"{ts()} [transcribe error] {result_holder['error']}", flush=True)
            reset_to_idle("STT error")
            continue

        text = result_holder.get("text", "").strip()
        if text:
            print(f"{ts()} [STT] {stt_elapsed:.2f}s → {text}")
            mqtt_publish(TOPIC_TRANSCRIPT_USER, text)
            word_count = len(text.split())
            if word_count <= 3:
                print(
                    f"{ts()} [transcribe] Too short ({word_count} words), discarding: {text!r}"
                )
                reset_to_idle("transcript too short")
            else:
                with state_lock:
                    listen_state = ListenState.RESPONDING
                mqtt_publish(TOPIC_STATE, "thinking")
                llm_queue.put(text)
        else:
            reset_to_idle("empty transcript")


def strip_markdown(text: str) -> str:
    """Remove markdown formatting, keeping only punctuation used in spoken conversation."""
    text = re.sub(r"#+\s*", "", text)  # headers
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)  # bold/italic
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)  # bold/italic (underscore)
    text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)  # inline code
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links [text](url)
    text = re.sub(r"^[>\-\*]\s*", "", text, flags=re.MULTILINE)  # blockquote/bullet
    text = re.sub(r"\s{2,}", " ", text)  # collapse multiple spaces
    return text.strip()


# Sentence splitting regex for streaming LLM output
# Prevents splitting on common abbreviations, numbers, or initials
ABBREV = (
    r"(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bSt)"
    r"(?<!\bvs)(?<!\betc)(?<!\be\.g)(?<!\bi\.e)"
)
NOT_INITIALS = r"(?<![A-Z])"
SENTENCE_END = re.compile(ABBREV + NOT_INITIALS + r"(?:[.!?](?=\s|$)|\.\.\.(?=\s))")


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """
    Extract complete sentences from buffer.
    Returns (ready_sentences, leftover_fragment).
    """
    sentences = []
    pos = 0
    for match in SENTENCE_END.finditer(buffer):
        end = match.end()
        sentences.append(buffer[pos:end])
        pos = end
    leftover = buffer[pos:]
    return sentences, leftover


def llm_loop() -> None:
    global listen_state, conversation_history

    llm_client = OpenAI(
        base_url=DEEPSEEK_BASE_URL,
        api_key=DEEPSEEK_API_KEY,
    )

    while not shutdown_event.is_set():
        try:
            transcript: str = llm_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if transcript is None:
            break
        tts_queued = False
        timed_out = False

        # Augment story requests with a randomly selected story before sending to
        # the LLM.  We store only the original transcript in history so the
        # SYSTEM NOTE doesn't pollute future turns.
        llm_transcript = augment_story_prompt(transcript)

        # Append the original (unaugmented) user transcript to conversation history
        with history_lock:
            conversation_history.append({"role": "user", "content": transcript})

        try:
            print(
                f"{ts()} [LLM] Sending to {LLM_MODEL}: {llm_transcript!r}", flush=True
            )
            # Build messages: history with the last user turn replaced by the
            # (possibly augmented) transcript so the SYSTEM NOTE reaches the LLM.
            with history_lock:
                messages_snapshot = list(conversation_history)
            # Replace the last entry (just-appended user turn) with the augmented one
            messages_snapshot[-1] = {"role": "user", "content": llm_transcript}
            stream = llm_client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": LLM_SYSTEM_PROMPT},
                ]
                + messages_snapshot,
                stream=True,
            )

            print(f"{ts()} [LLM] ", end="", flush=True)
            collected = ""
            buffer = ""
            stream_start = time.monotonic()
            last_token_time = time.monotonic()

            for chunk in stream:
                now = time.monotonic()

                # Check: no token for too long
                if now - last_token_time > LLM_TOKEN_TIMEOUT_S:
                    print(
                        f"\n{ts()} [LLM] TIMEOUT: no token for {LLM_TOKEN_TIMEOUT_S}s — aborting",
                        flush=True,
                    )
                    timed_out = True
                    break

                # Check: total time exceeded
                if now - stream_start > LLM_TOTAL_TIMEOUT_S:
                    print(
                        f"\n{ts()} [LLM] TIMEOUT: total stream exceeded {LLM_TOTAL_TIMEOUT_S}s — aborting",
                        flush=True,
                    )
                    timed_out = True
                    break

                token = chunk.choices[0].delta.content or ""
                if token:
                    collected += token
                    buffer += token
                    last_token_time = time.monotonic()
                    sys.stdout.write(token)
                    sys.stdout.flush()

                    sentences, buffer = split_sentences(buffer)
                    for sentence in sentences:
                        clean = strip_markdown(sentence).strip()
                        if clean:
                            tts_queue.put(clean)
                            tts_queued = True

            if timed_out:
                # Stream timed out — don't commit assistant reply; remove user turn
                with history_lock:
                    if (
                        conversation_history
                        and conversation_history[-1]["role"] == "user"
                    ):
                        conversation_history.pop()
                reset_to_idle("LLM timeout")
                continue

            # Flush any remaining text in the buffer as a final sentence
            if buffer.strip():
                clean = strip_markdown(buffer).strip()
                if clean:
                    tts_queue.put(clean)
                    tts_queued = True

            # Signal end of this LLM turn to the TTS pipeline so reset_to_idle
            # fires exactly once, not once per sentence.
            if tts_queued:
                tts_queue.put(TTS_TURN_DONE)

            # Log the full response for debugging
            sanitized = strip_markdown(collected)
            if sanitized != collected:
                print(f"\n{ts()} [LLM] Sanitized: {sanitized}")
            else:
                llm_elapsed = time.monotonic() - stream_start
                print(f"\n{ts()} [LLM] {llm_elapsed:.2f}s", flush=True)

            # Commit assistant reply to history (only if we have a real response)
            if collected:
                with history_lock:
                    conversation_history.append(
                        {"role": "assistant", "content": collected}
                    )
                    # Cap history to prevent unbounded context growth
                    if len(conversation_history) > CONVERSATION_HISTORY_MAX_TURNS:
                        # Keep only the most recent N message objects (system prompt is separate)
                        conversation_history[:] = conversation_history[
                            -CONVERSATION_HISTORY_MAX_TURNS:
                        ]
                    print(
                        f"{ts()} [LLM] History: {len(conversation_history)} messages stored."
                    )
                    if collected:
                        mqtt_publish(TOPIC_TRANSCRIPT_ASST, collected)

        except Exception as exc:
            print(f"{ts()} [LLM error] {exc}", flush=True)
            # Roll back the dangling user turn — no assistant reply was stored
            with history_lock:
                if conversation_history and conversation_history[-1]["role"] == "user":
                    conversation_history.pop()
        finally:
            if not tts_queued:
                reset_to_idle("no TTS queued")


def wav_bytes_to_float32(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Convert WAV bytes (16-bit PCM) to a float32 numpy array normalized to [-1, 1].
    Returns (pcm_float32, sample_rate_hz).
    """
    buf = io.BytesIO(wav_bytes)
    with wave.open(buf, "rb") as wf:
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return pcm / 32768.0, sample_rate


def send_audio_esp32(pcm_int16: np.ndarray) -> None:
    """Send int16 PCM audio to the ESP32 over UDP, paced to real-time.
    Uses deadline-based timing instead of sleep() to avoid drift.
    """
    chunk_duration = AUDIO_SEND_CHUNK / AUDIO_SEND_RATE  # 0.032s
    deadline = time.monotonic()

    for i in range(0, len(pcm_int16), AUDIO_SEND_CHUNK):
        chunk = pcm_int16[i : i + AUDIO_SEND_CHUNK]
        if len(chunk) < AUDIO_SEND_CHUNK:
            chunk = np.pad(chunk, (0, AUDIO_SEND_CHUNK - len(chunk)))
        audio_send_sock.sendto(chunk.tobytes(), (ESP32_IP, ESP32_AUDIO_PORT))

        deadline += chunk_duration
        now = time.monotonic()
        remaining = deadline - now
        if remaining > 0:
            time.sleep(remaining)
        # If remaining < 0, we're behind — skip sleep, catch up immediately


def mqtt_send_ctrl(payload: str) -> None:
    """Publish a control command to the ESP32 via MQTT.
    Replaces send_ctrl() and send_chime_stop().
    payload must be one of: "processing" | "stop"
    """
    try:
        mqtt_client.publish(TOPIC_CTRL, payload)
    except Exception as exc:
        print(f"{ts()} [CTRL] MQTT publish failed ({payload!r}): {exc}", flush=True)


def mqtt_publish(topic: str, payload: str) -> None:
    if not mqtt_client.is_connected():
        return
    try:
        mqtt_client.publish(topic, payload)
    except Exception as exc:
        print(
            f"{ts()} [MQTT] Publish failed ({topic!r} {payload!r}): {exc}", flush=True
        )


def reset_to_idle(reason: str = "") -> None:
    """
    Reset Python state and always tell ESP32 to turn off LED/chime.
    This is the main fix for the bug where the blue LED stays permanently ON
    after a false positive wake word or any non-speech pipeline abort.
    """
    global listen_state, accumulator, silence_packets, is_responding, leftover

    mqtt_send_ctrl("stop")

    with queue_lock:
        is_responding = False
        response_queue.clear()
        leftover = np.zeros(0, dtype=np.float32)

    accumulator = []
    silence_packets = 0

    if vad_model is not None:
        try:
            vad_model.reset_states()
        except Exception:
            pass

    with state_lock:
        listen_state = ListenState.IDLE
    mqtt_publish(TOPIC_STATE, "idle")

    if reason:
        print(f"{ts()} [STATE] Reset to IDLE: {reason}")
    else:
        print(f"{ts()} [STATE] Ready. Waiting for wake word...")


def play_audio_local(pcm_int16: np.ndarray) -> None:
    """Queue int16 PCM audio for local playback via sounddevice.
    PCM data is expected to be at 16kHz (resampled upstream in tts_loop).
    """
    global is_responding
    pcm_float = pcm_int16.astype(np.float32) / 32768.0
    with queue_lock:
        is_responding = True
        for i in range(0, len(pcm_float), SAMPLES_PER_PKT):
            chunk = pcm_float[i : i + SAMPLES_PER_PKT]
            if len(chunk) < SAMPLES_PER_PKT:
                chunk = np.pad(chunk, (0, SAMPLES_PER_PKT - len(chunk)))
            response_queue.append(chunk)


def play_audio(pcm_int16: np.ndarray) -> None:
    """Route int16 PCM audio to the configured output(s)."""
    if AUDIO_OUTPUT == "local":
        play_audio_local(pcm_int16)
    elif AUDIO_OUTPUT == "esp32":
        send_audio_esp32(pcm_int16)
    elif AUDIO_OUTPUT == "both":
        # Local playback is non-blocking (just queues), so run it first
        play_audio_local(pcm_int16)
        send_audio_esp32(pcm_int16)


# =============================================================================
# NEW: True Streaming LLM → TTS Pipeline
# =============================================================================
# The old tts_loop blocked on each sentence synthesis, meaning sentence N+1
# could not start synthesising until sentence N was fully rendered.  With a
# ThreadPoolExecutor we can overlap synthesis of consecutive sentences (up to
# TTS_MAX_WORKERS at a time).  An audio_collector_loop ensures playback still
# happens in the correct order.
# -----------------------------------------------------------------------------

TTS_MAX_WORKERS = 2  # Piper/ONNX releases the GIL, so 2 workers helps on multi-core

# Global Piper voice handle — loaded in main() before threads start
piper_voice: PiperVoice | None = None

# Monotonic sequence counter for sentences within a turn (resets each turn via
# the done-sentinel logic in audio_collector_loop)
tts_sequence = 0
tts_seq_lock = threading.Lock()

# Queue that carries (seq, audio_int16|Exception|None) from executor callbacks
# to the collector thread.  None means "end of turn".
audio_ready_queue: queue.Queue = queue.Queue()

# ThreadPoolExecutor for concurrent TTS synthesis
tts_executor: concurrent.futures.ThreadPoolExecutor | None = None


def synthesize_text(text: str) -> np.ndarray:
    """Synthesize a single text sentence to int16 PCM using Piper.

    This function is executed inside the ThreadPoolExecutor.  It keeps the
    same timeout, resampling and normalisation logic as the old tts_loop.
    """
    result_holder = {}

    def _do_synth():
        try:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav_file:
                piper_voice.synthesize_wav(text, wav_file)
            result_holder["audio"] = buf.getvalue()
        except Exception as exc:
            result_holder["error"] = exc

    # Run Piper synthesis in a sub-thread so we can enforce a hard timeout
    t = threading.Thread(target=_do_synth, daemon=True)
    t.start()
    t.join(timeout=TTS_TIMEOUT_S)

    if t.is_alive():
        raise TimeoutError(f"TTS synthesis timed out after {TTS_TIMEOUT_S}s")

    if "error" in result_holder:
        raise result_holder["error"]

    audio_bytes = result_holder["audio"]
    pcm_float, src_rate = wav_bytes_to_float32(audio_bytes)

    print(
        f"{ts()} [TTS] WAV: {src_rate}Hz, peak={np.max(np.abs(pcm_float)):.3f}",
        flush=True,
    )

    g = gcd(src_rate, AUDIO_SEND_RATE)
    up = AUDIO_SEND_RATE // g
    down = src_rate // g

    print(
        f"{ts()} [TTS] PCM sample rate: {src_rate}Hz → resampling {down}:{up} to {AUDIO_SEND_RATE}Hz",
        flush=True,
    )

    pcm_resampled = scipy.signal.resample_poly(pcm_float, up=up, down=down)

    # Normalize AFTER resampling (ringing can push peak above 1.0)
    peak = np.max(np.abs(pcm_resampled))
    if peak > 0:
        pcm_resampled = pcm_resampled / peak

    pcm_int16 = (pcm_resampled * 0.6 * 32767).clip(-32768, 32767).astype(np.int16)
    return pcm_int16


def tts_dispatcher_loop() -> None:
    """Consume text sentences from tts_queue and submit synthesis tasks.

    Each sentence gets a monotonic sequence number.  When the LLM turn ends
    a TTS_TURN_DONE sentinel is forwarded so audio_collector_loop knows to
    inject the playback sentinel (None) at the correct position in the stream.
    """
    global tts_sequence

    while not shutdown_event.is_set():
        try:
            text = tts_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if text is None:
            break

        if text is TTS_TURN_DONE:
            with tts_seq_lock:
                seq = tts_sequence
                tts_sequence += 1
            audio_ready_queue.put((seq, None))
            continue

        with tts_seq_lock:
            seq = tts_sequence
            tts_sequence += 1

        def _on_done(fut: concurrent.futures.Future, s: int):
            try:
                audio = fut.result()
                audio_ready_queue.put((s, audio))
            except Exception as exc:
                audio_ready_queue.put((s, exc))

        print(f"{ts()} [TTS] Queuing seq={seq} ({len(text)} chars)...", flush=True)
        future = tts_executor.submit(synthesize_text, text)
        future.add_done_callback(lambda f, s=seq: _on_done(f, s))

    # Graceful executor shutdown on exit
    if tts_executor is not None:
        tts_executor.shutdown(wait=False)


def audio_collector_loop() -> None:
    """Reorder TTS results by sequence and push them to audio_queue.

    Because sentences may finish synthesis out of order (shorter sentences
    render faster), we buffer out-of-order results and only release them to
    the playback thread when the next expected sequence number arrives.
    """
    next_seq = 0
    buffer: dict[int, np.ndarray | Exception | None] = {}

    while not shutdown_event.is_set():
        try:
            seq, item = audio_ready_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if seq != next_seq:
            buffer[seq] = item
            continue

        # Walk the sequential chain starting at next_seq
        while True:
            if item is None:
                # End-of-turn sentinel → tell playback to call reset_to_idle
                with audio_queue_lock:
                    audio_queue.append(None)
                audio_queue_event.set()
                next_seq += 1
            elif isinstance(item, Exception):
                print(f"{ts()} [TTS error] seq={next_seq}: {item}", flush=True)
                reset_to_idle("TTS synthesis error")
                next_seq += 1
            else:
                with audio_queue_lock:
                    audio_queue.append(item)
                audio_queue_event.set()
                print(
                    f"{ts()} [TTS] seq={next_seq} → queued {len(item)} samples ({AUDIO_OUTPUT})",
                    flush=True,
                )
                next_seq += 1

            if next_seq in buffer:
                item = buffer.pop(next_seq)
            else:
                break


def audio_dispatch_loop() -> None:
    """Drain audio_queue and dispatch each sentence for playback.
    Runs in its own daemon thread, decoupled from TTS synthesis so the
    next sentence can be synthesised while the current one plays.
    Uses a None sentinel to call reset_to_idle exactly once per LLM turn.
    """
    while not shutdown_event.is_set():
        audio_queue_event.wait(timeout=0.1)
        while True:
            with audio_queue_lock:
                if not audio_queue:
                    break
                item = audio_queue.popleft()
            if item is None:
                # End-of-turn sentinel — reset state once, not once per sentence
                time.sleep(
                    0.25
                )  # drain delay: let ESP32 I2S DMA finish playing last packet
                reset_to_idle("ESP32 playback finished")
            else:
                mqtt_publish(TOPIC_STATE, "speaking")
                play_audio(item)
        audio_queue_event.clear()


def receive_loop(sock: socket.socket) -> None:
    """Background thread: receive UDP packets and enqueue decoded audio."""
    expected_bytes = SAMPLES_PER_PKT * 2  # uint16 = 2 bytes each
    while not shutdown_event.is_set():
        try:
            data, _ = sock.recvfrom(expected_bytes * 2)
        except socket.timeout:
            continue
        if len(data) != expected_bytes:
            continue
        raw = np.frombuffer(data, dtype="<u2").astype(np.float32)
        audio = (raw - 2048.0) / 2048.0
        audio = audio - np.mean(audio)

        # Noise gate: only applies to playback if you want to suppress idle hiss.
        # For full-fidelity recording/monitoring, set NOISE_GATE = 0 or remove this block.
        if NOISE_GATE > 0 and np.sqrt(np.mean(audio**2)) < NOISE_GATE:
            playback_audio = np.zeros(SAMPLES_PER_PKT, dtype=np.float32)
        else:
            playback_audio = audio

        with queue_lock:
            if len(packet_queue) >= MAX_QUEUE_LEN:
                packet_queue.popleft()
            packet_queue.append(playback_audio)  # <-- was using gated audio
            vad_queue.append(audio)
            if len(vad_queue) > MAX_QUEUE_LEN * 4:
                vad_queue.popleft()


def audio_callback(outdata: np.ndarray, frames: int, time, status) -> None:
    """sounddevice callback: fill outdata with queued audio, silence on underrun."""
    global leftover, is_responding, listen_state

    output = np.zeros(frames, dtype=np.float32)
    write_pos = 0
    needed = frames

    with queue_lock:
        responding = is_responding

    if responding:
        # Drain TTS audio from response_queue
        if len(leftover) > 0:
            use = min(len(leftover), needed)
            output[write_pos : write_pos + use] = leftover[:use]
            leftover = leftover[use:]
            write_pos += use
            needed -= use

        while needed > 0:
            with queue_lock:
                if not response_queue:
                    break
                if response_queue[0] is None:
                    response_queue.popleft()  # discard this sentinel
                    # Only stop if there's nothing else queued
                    if not response_queue:
                        is_responding = False
                        leftover = np.zeros(0, dtype=np.float32)
                        with state_lock:
                            listen_state = ListenState.IDLE
                    # Either way, stop filling this callback frame
                    break
                chunk = response_queue.popleft()

            if len(chunk) <= needed:
                output[write_pos : write_pos + len(chunk)] = chunk
                write_pos += len(chunk)
                needed -= len(chunk)
            else:
                output[write_pos : write_pos + needed] = chunk[:needed]
                leftover = chunk[needed:]
                needed = 0
    else:
        # Normal mic passthrough — existing logic unchanged
        if len(leftover) > 0:
            use = min(len(leftover), needed)
            output[write_pos : write_pos + use] = leftover[:use]
            leftover = leftover[use:]
            write_pos += use
            needed -= use

        while needed > 0:
            with queue_lock:
                if not packet_queue:
                    break  # note: should be break, not continue
                chunk = packet_queue.popleft()
            if len(chunk) <= needed:
                output[write_pos : write_pos + len(chunk)] = chunk
                write_pos += len(chunk)
                needed -= len(chunk)
            else:
                # Chunk is larger than remaining space — save the tail for next callback
                output[write_pos : write_pos + needed] = chunk[:needed]
                leftover = chunk[needed:]
                needed = 0

    outdata[:, 0] = output


def warmup_llm() -> None:
    """
    Fire a single non-streaming request to DeepSeek on startup to cache the
    system prompt and eliminate cold-start latency on the first real user turn.
    The response is discarded — this is purely a cache-priming call.
    """
    print(f"{ts()} [LLM] Warming up DeepSeek (caching system prompt)...", flush=True)
    t0 = time.monotonic()
    try:
        llm_client = OpenAI(
            base_url=DEEPSEEK_BASE_URL,
            api_key=DEEPSEEK_API_KEY,
        )
        llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Hey Elio, this is a warmup message from the system — no need to respond with anything meaningful, just making sure you're ready to go.",
                },
            ],
            max_tokens=16,  # we don't care about the reply, keep it tiny
            stream=False,
        )
        elapsed = time.monotonic() - t0
        print(
            f"{ts()} [LLM] Warmup complete ({elapsed:.2f}s) — system prompt cached.",
            flush=True,
        )
    except Exception as exc:
        print(f"{ts()} [LLM] Warmup failed ({exc}) — continuing anyway.", flush=True)


def main() -> None:
    global ESP32_IP, piper_voice, tts_executor

    if RECORDING_MODE:
        print(
            f"{ts()} *** RECORDING MODE ACTIVE — all voice pipeline threads disabled ***"
        )
    else:
        print(
            f"{ts()} Using Groq STT model '{GROQ_MODEL}', Piper TTS model '{PIPER_MODEL_PATH}'"
        )
        load_silero_vad()

    # Broadcast this machine as raspberrypi.local on Windows (no-op on Linux/Pi)
    start_windows_mdns_broadcast("raspberrypi")

    # Resolve ESP32's mDNS hostname to an IP for sending TTS audio back
    ESP32_IP = resolve_mdns(ESP32_MDNS_HOST)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(1.0)
    print(f"{ts()} Listening for UDP audio on port {UDP_PORT}...")

    # MQTT startup — always active (wake signals must work in RECORDING_MODE too
    # if you ever want them; in normal mode this is required for wake word)
    mqtt_client.on_message = on_mqtt_message
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
    mqtt_client.subscribe(TOPIC_WAKE)
    mqtt_client.loop_start()  # spawns a daemon thread; no manual thread needed
    print(
        f"{ts()} MQTT client connected to {MQTT_BROKER}:{MQTT_PORT}, "
        f"subscribed to {TOPIC_WAKE}"
    )

    # On startup, force-clear any LED / chime state left over from a previous run
    # where the script was killed before it could send "stop" to the ESP32.
    time.sleep(0.1)  # let the MQTT loop_start thread establish the session
    mqtt_send_ctrl("stop")
    print(f"{ts()} Sent startup 'stop' to clear any stale LED/chime state.")

    # Load Piper voice and create TTS executor before spawning worker threads
    if not RECORDING_MODE:
        print(f"{ts()} Loading Piper TTS voice...")
        piper_voice = PiperVoice.load(PIPER_MODEL_PATH)
        tts_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=TTS_MAX_WORKERS
        )
        print(f"{ts()} Piper loaded. TTS executor: {TTS_MAX_WORKERS} workers.")

    # Start all background threads
    threads = []
    for target, args in [
        (receive_loop, (sock,)),
    ]:
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()
        threads.append(t)

    if not RECORDING_MODE:
        for target, args in [
            (vad_accumulator_loop, ()),
            (transcription_loop, ()),
            (llm_loop, ()),
            (tts_dispatcher_loop, ()),
            (audio_collector_loop, ()),
            (audio_dispatch_loop, ()),
        ]:
            t = threading.Thread(target=target, args=args, daemon=True)
            t.start()
            threads.append(t)

        warmup_llm()

        print(f"{ts()} Waiting for {PREBUFFER_PKTS} packets to pre-buffer...")
    deadline = time.monotonic() + 10.0  # wait at most 10 seconds
    while True:
        with queue_lock:
            if len(packet_queue) >= PREBUFFER_PKTS:
                break
        if time.monotonic() > deadline:
            print(f"{ts()} WARNING: No audio from ESP32 after 10s — continuing anyway.")
            break
        time.sleep(0.01)

    mqtt_publish(TOPIC_SYSTEM_READY, "1")
    print(f"{ts()} Starting playback. Press Ctrl+C to stop.")
    with sd.OutputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        callback=audio_callback,
        blocksize=SAMPLES_PER_PKT,
    ):
        try:
            while not shutdown_event.is_set():
                sd.sleep(200)
        except KeyboardInterrupt:
            print(f"\n{ts()} Shutting down...")

        shutdown_event.set()

        # Tell ESP32 to turn off LED / stop chime
        mqtt_publish(TOPIC_SYSTEM_SHUTDOWN, "1")
        mqtt_send_ctrl("stop")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

        # Unblock any thread stuck on queue.get() with sentinel values
        llm_queue.put(None)
        tts_queue.put(None)
        transcribe_queue.put(None)

        for t in threads:
            t.join(timeout=3.0)

        # Shut down the TTS executor cleanly
        if tts_executor is not None:
            tts_executor.shutdown(wait=False)

        print(f"{ts()} All threads stopped. Goodbye.")


if __name__ == "__main__":
    main()
