import platform
import threading
from pathlib import Path

import cv2
import numpy as np
import paho.mqtt.client as mqtt
from flask import Flask, Response, jsonify, render_template, request

# picamera2 is only available on Linux/Raspberry Pi — import lazily so the
# module can still be loaded on Windows without the package installed.
if platform.system() == "Linux":
    from picamera2 import Picamera2

app = Flask(__name__)

# Download these from the OpenCV zoo:
# https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
# https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface
YUNET_MODEL = "models/face_detection_yunet_2023mar.onnx"
SFACE_MODEL = "models/face_recognition_sface_2021dec.onnx"

COSINE_THRESHOLD = 0.363  # below this → different person

# Max distance (px) between a face's bbox center this frame and last frame
# before we treat it as the same person and reuse the cached label.
# Below this: reuse cached label (skip SFace CNN). Above: re-identify.
MATCH_DIST_PX = 60

# --- Performance tuning ---
# Fixed capture resolution on both PC and Raspberry Pi.
CAM_WIDTH = 640
CAM_HEIGHT = 480
CAM_FPS = 30

# YuNet runs on a downscaled copy of the frame for speed; detections are
# rescaled back up to full CAM_WIDTH x CAM_HEIGHT before use elsewhere
# (annotation, alignCrop, enrollment all stay in full-res coordinates).
DETECT_WIDTH = 320

# JPEG quality for the MJPEG stream (0-100). Lower = smaller frames = smoother
# playback over the network, at the cost of some visual quality.
MJPEG_QUALITY = 50

# Width to downscale the annotated frame to before sending over the MJPEG
# stream. Capture/detection/enrollment all stay at full CAM_WIDTH resolution —
# only the outgoing stream is shrunk, to cut bandwidth further.
STREAM_WIDTH = 320

# Persistent face database path
FACES_DB = Path("faces_db.npz")

# ---- MQTT ----
MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
TOPIC_FACE_SEEN = "elio/face/seen"  # payload: the display name of the recognised person
TOPIC_ROBOT_CMD = "luna/robot/cmd"  # payload: robot drive/mode commands
TOPIC_ROBOT_EMOTION = "luna/robot/emotion"  # payload: OLED emotion name

MODE_MANUAL = "MODE-2"
CMD_FORWARD = "MANUAL:FORWARD"
CMD_STOP = "MANUAL:STOP"

EMOTION_KNOWN_FACE = "LOVE"
EMOTION_DEFAULT = "HAPPY"

# Number of consecutive frames a known face must be seen in before we
# trigger the forward-drive command (debounces flicker/misidentification).
FORWARD_TRIGGER_FRAMES = 3

_mqtt_client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="elio-camera",
)
_mqtt_client.reconnect_delay_set(min_delay=1, max_delay=10)
_mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT)
_mqtt_client.loop_start()


def _mqtt_publish(topic: str, payload: str) -> None:
    if not _mqtt_client.is_connected():
        return
    try:
        _mqtt_client.publish(topic, payload)
    except Exception as exc:
        print(f"[MQTT] Publish failed ({topic!r} {payload!r}): {exc}", flush=True)


def _send_robot_drive_cmd(payload: str) -> None:
    """Switch the robot into manual control mode, then send a drive command.
    Mirrors receiver.py's pattern of sending MODE-2 before any MANUAL:* cmd."""
    _mqtt_publish(TOPIC_ROBOT_CMD, MODE_MANUAL)
    _mqtt_publish(TOPIC_ROBOT_CMD, payload)


# ---------------


class FacialRecognition:
    def __init__(self):
        self.HOST_OS = platform.system()
        self.latest_frame = None
        # Snapshot of (raw_frame, faces) from the most recent detection pass.
        # Enrollment reads this so it reuses the *same* detection result that
        # produced the bounding box visible on screen — no second detect() call.
        self._latest_raw: tuple[np.ndarray, any] | None = None
        self._lock = threading.Lock()

        # --- Camera init: OpenCV on Windows, picamera2 on Linux (Pi) ---
        if self.HOST_OS == "Linux":
            self._picam = Picamera2()
            # RGB888 gives us a plain HxWx3 uint8 array; we convert to BGR for OpenCV.
            config = self._picam.create_video_configuration(
                main={"format": "RGB888", "size": (CAM_WIDTH, CAM_HEIGHT)},
                controls={"FrameRate": CAM_FPS},
            )
            self._picam.configure(config)
            self._picam.start()
            self._cap = None
            print(
                f"Camera: picamera2 (Raspberry Pi) @ {CAM_WIDTH}x{CAM_HEIGHT}@{CAM_FPS}"
            )
        else:
            self._picam = None
            self._cap = cv2.VideoCapture(3)  # OBS Virtual Camera
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_WIDTH)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_HEIGHT)
            self._cap.set(cv2.CAP_PROP_FPS, CAM_FPS)
            print(
                f"Camera: OpenCV VideoCapture (Windows) @ {CAM_WIDTH}x{CAM_HEIGHT}@{CAM_FPS}"
            )

        # --- YuNet detector ---
        # Initial inputSize is set to the downscaled detection resolution;
        # _detect_faces() calls setInputSize() per-frame anyway, but this
        # gives a sane default matching how it's actually used.
        self._detector = cv2.FaceDetectorYN.create(
            YUNET_MODEL,
            "",
            (DETECT_WIDTH, round(DETECT_WIDTH * CAM_HEIGHT / CAM_WIDTH)),
            score_threshold=0.6,
            nms_threshold=0.3,
            top_k=5000,
        )

        # --- SFace recognizer ---
        self._recognizer = cv2.FaceRecognizerSF.create(SFACE_MODEL, "")

        # known_faces: { "name_lower": { "display": str, "embeddings": list[np.ndarray] } }
        self._known_faces: dict[str, dict] = {}
        # Per-face tracking cache for _annotate: (cx, cy, label) for each face
        # seen in the previous frame, used to skip the expensive SFace forward
        # pass when a face hasn't moved far enough to be a new person.
        self._tracked_faces: list[tuple[float, float, str]] = []
        # Incremented on every enrollment so _annotate knows to bust the cache.
        self._db_version: int = 0
        self._tracked_faces_db_version: int = 0
        # Names (display) that have already triggered an MQTT face/seen pub this
        # session.  Reset only when the process restarts, so each person gets
        # greeted at most once per run.
        self._greeted: set[str] = set()
        # Consecutive-frame counter for "a known face is currently visible",
        # and whether we've already told the robot to drive forward as a
        # result (so we send FORWARD/STOP exactly once on each transition).
        self._known_face_streak: int = 0
        self._robot_driving: bool = False
        self._load_db()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_db(self):
        if not FACES_DB.exists():
            return
        data = np.load(FACES_DB, allow_pickle=True)
        keys = data["keys"].tolist()  # list of lowercase name keys
        displays = data["displays"].tolist()  # list of display names
        embeddings = data["embeddings"].tolist()  # list of np arrays

        for key, display, emb in zip(keys, displays, embeddings):
            if key not in self._known_faces:
                self._known_faces[key] = {"display": display, "embeddings": []}
            self._known_faces[key]["embeddings"].append(np.array(emb, dtype=np.float32))

        print(
            f"Loaded {sum(len(v['embeddings']) for v in self._known_faces.values())} embeddings for {len(self._known_faces)} people."
        )

    def _save_db(self):
        keys, displays, embeddings = [], [], []
        for key, entry in self._known_faces.items():
            for emb in entry["embeddings"]:
                keys.append(key)
                displays.append(entry["display"])
                embeddings.append(emb)
        np.savez(
            FACES_DB,
            keys=np.array(keys),
            displays=np.array(displays),
            embeddings=np.array(embeddings, dtype=object),
        )

    # ------------------------------------------------------------------
    # Public: enroll from the live webcam frame
    # ------------------------------------------------------------------
    def enroll_from_frame(self, name: str) -> tuple[bool, str]:
        """
        Use the most recent raw frame + detection result from read_cam to
        enroll a face under `name`.  Reusing the cached detection avoids the
        race where a second detect() call on a slightly-later frame misses the
        face that was visible on screen when the user clicked Enroll.
        Returns (success, message).
        """
        with self._lock:
            snapshot = self._latest_raw

        if snapshot is None:
            return False, "No frame available from camera."

        frame, faces = snapshot

        if faces is None or len(faces) == 0:
            return False, "No face detected in frame."

        # Pick the largest face by bounding-box area
        largest = max(faces, key=lambda f: f[2] * f[3])

        aligned = self._recognizer.alignCrop(frame, largest)
        embedding = self._recognizer.feature(aligned)

        key = name.strip().lower()
        display = name.strip()

        if key not in self._known_faces:
            self._known_faces[key] = {"display": display, "embeddings": []}

        self._known_faces[key]["embeddings"].append(embedding)
        self._db_version += 1
        self._save_db()

        count = len(self._known_faces[key]["embeddings"])
        print(f"Enrolled: {display!r} (now has {count} photo(s))")
        return True, f"Enrolled {display!r} ({count} photo(s) total)."

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _detect_faces(self, frame: np.ndarray):
        """Run YuNet on a downscaled copy of `frame` for speed, then rescale
        the resulting boxes/landmarks back up to the original frame's
        coordinate space. Returns the same `faces` array shape that
        detector.detect() would, just computed faster on a smaller image.
        """
        h, w = frame.shape[:2]

        if w <= DETECT_WIDTH:
            # Already small enough — detect at full res, no scaling needed.
            self._detector.setInputSize((w, h))
            _, faces = self._detector.detect(frame)
            return faces

        scale = DETECT_WIDTH / w
        small_w = DETECT_WIDTH
        small_h = max(1, round(h * scale))
        small = cv2.resize(frame, (small_w, small_h), interpolation=cv2.INTER_LINEAR)

        self._detector.setInputSize((small_w, small_h))
        _, faces = self._detector.detect(small)

        if faces is None or len(faces) == 0:
            return faces

        # Rescale all columns except the last (the detection confidence score)
        # back up to full-frame coordinates. Columns are: x, y, w, h, then 5
        # landmark (x, y) pairs, then score.
        faces = faces.copy()
        inv_scale = 1.0 / scale
        faces[:, :-1] *= inv_scale
        return faces

    def _get_embedding(self, frame: np.ndarray) -> np.ndarray | None:
        """Detect first face in frame and return its SFace embedding."""
        faces = self._detect_faces(frame)
        if faces is None or len(faces) == 0:
            return None
        aligned = self._recognizer.alignCrop(frame, faces[0])
        return self._recognizer.feature(aligned)

    def _identify(self, embedding: np.ndarray) -> str:
        """Cosine-match embedding against all enrolled embeddings (multi-photo support)."""
        best_name = "Unknown"
        best_score = -1.0
        for key, entry in self._known_faces.items():
            for known_emb in entry["embeddings"]:
                score = self._recognizer.match(
                    embedding, known_emb, cv2.FaceRecognizerSF_FR_COSINE
                )
                if score > best_score:
                    best_score = score
                    best_name = entry["display"]
        if best_score < COSINE_THRESHOLD:
            return "Unknown"
        return f"{best_name} ({best_score:.2f})"

    def _update_robot_drive_state(self, known_face_present: bool) -> None:
        """Called once per processed frame with whether a known face is
        currently visible anywhere in frame. Drives the robot forward after
        FORWARD_TRIGGER_FRAMES consecutive frames with a known face, and
        stops it as soon as no known face is visible."""
        if known_face_present:
            self._known_face_streak += 1
            if (
                self._known_face_streak >= FORWARD_TRIGGER_FRAMES
                and not self._robot_driving
            ):
                self._robot_driving = True
                _send_robot_drive_cmd(CMD_FORWARD)
                _mqtt_publish(TOPIC_ROBOT_EMOTION, EMOTION_KNOWN_FACE)
                print(
                    "[ROBOT] Known face confirmed — sending FORWARD + LOVE",
                    flush=True,
                )
        else:
            self._known_face_streak = 0
            if self._robot_driving:
                self._robot_driving = False
                _send_robot_drive_cmd(CMD_STOP)
                _mqtt_publish(TOPIC_ROBOT_EMOTION, EMOTION_DEFAULT)
                print("[ROBOT] Known face lost — sending STOP + HAPPY", flush=True)

    def _annotate(self, frame: np.ndarray, faces) -> np.ndarray:
        """Draw bounding boxes and identity labels on frame.

        Reuses cached labels from the previous frame for faces whose bbox
        center hasn't moved more than MATCH_DIST_PX — only "new" faces
        (by position) pay the full SFace feature() cost.
        """
        if faces is None:
            self._tracked_faces = []
            self._update_robot_drive_state(False)
            return frame

        # If the face DB was updated since we last ran (e.g. a new enroll),
        # clear the position cache so every face gets re-identified immediately.
        if self._tracked_faces_db_version != self._db_version:
            self._tracked_faces = []
            self._tracked_faces_db_version = self._db_version

        new_tracked: list[tuple[float, float, str]] = []
        used: set[int] = set()
        known_face_present = False

        for face in faces:
            x, y, w, h = (int(v) for v in face[:4])
            cx, cy = x + w / 2, y + h / 2

            # Find the closest unclaimed face we tracked last frame
            best_i, best_d = -1, MATCH_DIST_PX
            for i, (px, py, _label) in enumerate(self._tracked_faces):
                if i in used:
                    continue
                d = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
                if d < best_d:
                    best_d = d
                    best_i = i

            if best_i >= 0:
                label = self._tracked_faces[best_i][2]
                used.add(best_i)
            else:
                # New face — run the expensive recognition path
                aligned = self._recognizer.alignCrop(frame, face)
                emb = self._recognizer.feature(aligned)
                label = self._identify(emb)

            new_tracked.append((cx, cy, label))

            color = (0, 255, 0) if not label.startswith("Unknown") else (0, 0, 255)

            # Fire MQTT exactly once per session for each known face.
            if not label.startswith("Unknown"):
                known_face_present = True
                # label is "<DisplayName> (0.xx)" — extract just the name part.
                display_name = label.rsplit(" (", 1)[0]
                if display_name not in self._greeted:
                    self._greeted.add(display_name)
                    _mqtt_publish(TOPIC_FACE_SEEN, display_name)
                    print(
                        f"[FACE] First sighting of {display_name!r} this session — "
                        f"published to {TOPIC_FACE_SEEN}",
                        flush=True,
                    )
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            cv2.putText(
                frame, label, (x, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2
            )

        self._tracked_faces = new_tracked
        self._update_robot_drive_state(known_face_present)
        return frame

    # ------------------------------------------------------------------
    # Main capture loop
    # ------------------------------------------------------------------
    def _process_frame(self, frame: np.ndarray):
        """Run detection + annotation on one frame and stash results."""
        faces = self._detect_faces(frame)

        # Stash the *raw* frame + detection result before annotation so
        # enroll_from_frame can reuse this exact detection (no race).
        raw_snapshot = (frame.copy(), faces)

        annotated = self._annotate(frame, faces)

        with self._lock:
            self.latest_frame = annotated
            self._latest_raw = raw_snapshot

    def read_cam(self):
        if self.HOST_OS == "Linux":
            self._read_cam_picamera2()
        else:
            self._read_cam_opencv()

    def _read_cam_opencv(self):
        """Capture loop using OpenCV VideoCapture (Windows / generic USB cam)."""
        if not self._cap.isOpened():
            print("Error: Could not open webcam.")
            return

        while True:
            ret, frame = self._cap.read()
            if not ret:
                break
            self._process_frame(frame)

    def _read_cam_picamera2(self):
        """Capture loop using picamera2 (Raspberry Pi camera module)."""
        while True:
            frame = self._picam.capture_array()
            # capture_array() with RGB888 returns BGR-ordered data in practice —
            # do NOT convert, just pass directly to OpenCV.
            self._process_frame(frame)

    def generate_mjpeg(self):
        import time

        while True:
            try:
                with self._lock:
                    frame = self.latest_frame
                if frame is None:
                    time.sleep(0.01)
                    continue

                # Annotated frame stays at full CAM_WIDTH/CAM_HEIGHT for
                # detection/enrollment quality; only the outgoing stream
                # copy is shrunk down to STREAM_WIDTH to save bandwidth.
                h, w = frame.shape[:2]
                if w > STREAM_WIDTH:
                    stream_h = max(1, round(h * STREAM_WIDTH / w))
                    frame = cv2.resize(
                        frame, (STREAM_WIDTH, stream_h), interpolation=cv2.INTER_AREA
                    )

                ok, buf = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, MJPEG_QUALITY]
                )
                if not ok:
                    time.sleep(0.01)
                    continue
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + buf.tobytes()
                    + b"\r\n"
                )
            except Exception as e:
                print(f"[MJPEG] stream error (continuing): {e}")
                time.sleep(0.01)


recognition = FacialRecognition()


@app.route("/stream")
def stream():
    return Response(
        recognition.generate_mjpeg(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/enroll", methods=["POST"])
def enroll():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "message": "Name cannot be empty."}), 400
    ok, message = recognition.enroll_from_frame(name)
    status = 200 if ok else 422
    return jsonify({"ok": ok, "message": message}), status


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    cam_thread = threading.Thread(target=recognition.read_cam, daemon=True)
    cam_thread.start()
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
