import platform
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import paho.mqtt.client as mqtt
from flask import Flask, Response, jsonify, render_template, request

if platform.system() == "Linux":
    from picamera2 import Picamera2

app = Flask(__name__)

YUNET_MODEL = "models/face_detection_yunet_2023mar.onnx"
SFACE_MODEL = "models/face_recognition_sface_2021dec.onnx"

COSINE_THRESHOLD = 0.363
MATCH_DIST_PX = 60
FACES_DB = Path("faces_db.npz")

CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_FPS = 30

PROCESS_WIDTH = 320
JPEG_QUALITY = 70

MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883

TOPIC_FACE_SEEN = "elio/face/seen"
TOPIC_ROBOT_CMD = "luna/robot/cmd"

RECOGNIZED_FRAMES_REQUIRED = 3

_last_robot_cmd = ""

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


def send_robot_cmd(cmd: str):
    global _last_robot_cmd

    if cmd != _last_robot_cmd:
        _mqtt_publish(TOPIC_ROBOT_CMD, cmd)
        print("Robot cmd:", cmd, flush=True)
        _last_robot_cmd = cmd


class FacialRecognition:
    def __init__(self):
        self.HOST_OS = platform.system()
        self.latest_frame = None
        self._latest_raw = None
        self._lock = threading.Lock()

        self._recognized_stable_count = 0
        self._last_recognized_name = None

        if self.HOST_OS == "Linux":
            self._picam = Picamera2()

            config = self._picam.create_video_configuration(
                main={
                    "format": "RGB888",
                    "size": (CAMERA_WIDTH, CAMERA_HEIGHT),
                },
                controls={"FrameRate": CAMERA_FPS},
            )

            self._picam.configure(config)
            self._picam.start()
            self._cap = None
            print("Camera: picamera2 Raspberry Pi low-latency mode")

        else:
            self._picam = None
            self._cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)

            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
            self._cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            print("Camera: OpenCV VideoCapture low-latency mode")

        self._detector = cv2.FaceDetectorYN.create(
            YUNET_MODEL,
            "",
            (PROCESS_WIDTH, int(PROCESS_WIDTH * CAMERA_HEIGHT / CAMERA_WIDTH)),
            score_threshold=0.6,
            nms_threshold=0.3,
            top_k=5000,
        )

        self._recognizer = cv2.FaceRecognizerSF.create(SFACE_MODEL, "")

        self._known_faces = {}
        self._tracked_faces = []
        self._db_version = 0
        self._tracked_faces_db_version = 0
        self._greeted = set()

        self._load_db()

        _mqtt_publish(TOPIC_ROBOT_CMD, "MODE:1")
        time.sleep(0.1)
        send_robot_cmd("STOP")

    def _load_db(self):
        if not FACES_DB.exists():
            print("No faces_db.npz found. Enroll known people first.")
            return

        data = np.load(FACES_DB, allow_pickle=True)
        keys = data["keys"].tolist()
        displays = data["displays"].tolist()
        embeddings = data["embeddings"].tolist()

        for key, display, emb in zip(keys, displays, embeddings):
            if key not in self._known_faces:
                self._known_faces[key] = {
                    "display": display,
                    "embeddings": [],
                }

            self._known_faces[key]["embeddings"].append(np.array(emb, dtype=np.float32))

        print(
            f"Loaded {sum(len(v['embeddings']) for v in self._known_faces.values())} "
            f"embeddings for {len(self._known_faces)} people."
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

    def enroll_from_frame(self, name: str):
        with self._lock:
            snapshot = self._latest_raw

        if snapshot is None:
            return False, "No frame available from camera."

        frame, faces = snapshot

        if faces is None or len(faces) == 0:
            return False, "No face detected in frame."

        largest = max(faces, key=lambda f: f[2] * f[3])

        aligned = self._recognizer.alignCrop(frame, largest)
        embedding = self._recognizer.feature(aligned)

        key = name.strip().lower()
        display = name.strip()

        if key not in self._known_faces:
            self._known_faces[key] = {
                "display": display,
                "embeddings": [],
            }

        self._known_faces[key]["embeddings"].append(embedding)
        self._db_version += 1
        self._save_db()

        count = len(self._known_faces[key]["embeddings"])
        print(f"Enrolled: {display!r} now has {count} photo(s)")

        return True, f"Enrolled {display!r} ({count} photo(s) total)."

    def _identify_with_score(self, embedding):
        best_name = "Unknown"
        best_score = -1.0

        for key, entry in self._known_faces.items():
            for known_emb in entry["embeddings"]:
                score = self._recognizer.match(
                    embedding,
                    known_emb,
                    cv2.FaceRecognizerSF_FR_COSINE,
                )

                if score > best_score:
                    best_score = score
                    best_name = entry["display"]

        if best_score < COSINE_THRESHOLD:
            return "Unknown", best_score

        return best_name, best_score

    def _identify(self, embedding):
        name, score = self._identify_with_score(embedding)

        if name == "Unknown":
            return "Unknown"

        return f"{name} ({score:.2f})"

    def _recognized_name_from_faces(self, frame, faces):
        if faces is None or len(faces) == 0:
            return None

        sorted_faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)

        for face in sorted_faces:
            try:
                aligned = self._recognizer.alignCrop(frame, face)
                emb = self._recognizer.feature(aligned)
                name, score = self._identify_with_score(emb)

                if name != "Unknown":
                    return name

            except Exception as e:
                print(f"[Robot recognition check] Error: {e}", flush=True)

        return None

    def _update_robot_command(self, frame, faces):
        recognized_name = self._recognized_name_from_faces(frame, faces)

        if recognized_name is None:
            self._recognized_stable_count = 0
            self._last_recognized_name = None
            send_robot_cmd("STOP")
            return

        if recognized_name == self._last_recognized_name:
            self._recognized_stable_count += 1
        else:
            self._last_recognized_name = recognized_name
            self._recognized_stable_count = 1

        if self._recognized_stable_count >= RECOGNIZED_FRAMES_REQUIRED:
            print(f"Known person detected: {recognized_name}")
            send_robot_cmd("FORWARD")
        else:
            send_robot_cmd("STOP")

    def _scale_faces_to_original(self, faces, scale_x, scale_y):
        if faces is None:
            return None

        scaled_faces = faces.copy()

        scaled_faces[:, 0] *= scale_x
        scaled_faces[:, 1] *= scale_y
        scaled_faces[:, 2] *= scale_x
        scaled_faces[:, 3] *= scale_y

        for i in range(4, scaled_faces.shape[1], 2):
            scaled_faces[:, i] *= scale_x

            if i + 1 < scaled_faces.shape[1]:
                scaled_faces[:, i + 1] *= scale_y

        return scaled_faces

    def _annotate(self, frame, faces):
        if faces is None:
            self._tracked_faces = []
            return frame

        if self._tracked_faces_db_version != self._db_version:
            self._tracked_faces = []
            self._tracked_faces_db_version = self._db_version

        new_tracked = []
        used = set()

        for face in faces:
            x, y, w, h = (int(v) for v in face[:4])
            cx, cy = x + w / 2, y + h / 2

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
                aligned = self._recognizer.alignCrop(frame, face)
                emb = self._recognizer.feature(aligned)
                label = self._identify(emb)

            new_tracked.append((cx, cy, label))

            if label.startswith("Unknown"):
                color = (0, 0, 255)
            else:
                color = (0, 255, 0)
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
                frame,
                label,
                (x, max(20, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                color,
                2,
            )

        self._tracked_faces = new_tracked
        return frame

    def _process_frame(self, frame):
        original_h, original_w = frame.shape[:2]

        process_h = int(PROCESS_WIDTH * original_h / original_w)

        small_frame = cv2.resize(
            frame,
            (PROCESS_WIDTH, process_h),
            interpolation=cv2.INTER_LINEAR,
        )

        self._detector.setInputSize((PROCESS_WIDTH, process_h))
        _, small_faces = self._detector.detect(small_frame)

        scale_x = original_w / PROCESS_WIDTH
        scale_y = original_h / process_h

        faces = self._scale_faces_to_original(small_faces, scale_x, scale_y)

        self._update_robot_command(frame, faces)

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
        if not self._cap.isOpened():
            print("Error: Could not open webcam.")
            return

        while True:
            for _ in range(2):
                self._cap.grab()

            ret, frame = self._cap.read()

            if not ret:
                continue

            self._process_frame(frame)

    def _read_cam_picamera2(self):
        while True:
            frame = self._picam.capture_array()
            self._process_frame(frame)

    def generate_mjpeg(self):
        encode_params = [
            int(cv2.IMWRITE_JPEG_QUALITY),
            JPEG_QUALITY,
        ]

        while True:
            try:
                with self._lock:
                    frame = self.latest_frame

                if frame is None:
                    time.sleep(0.005)
                    continue

                ok, buf = cv2.imencode(".jpg", frame, encode_params)

                if not ok:
                    time.sleep(0.005)
                    continue

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
                )

            except Exception as e:
                print(f"[MJPEG] stream error continuing: {e}")
                time.sleep(0.005)


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


def cleanup():
    try:
        send_robot_cmd("STOP")
        time.sleep(0.2)
    except Exception:
        pass

    try:
        _mqtt_client.loop_stop()
        _mqtt_client.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        cam_thread = threading.Thread(target=recognition.read_cam, daemon=True)
        cam_thread.start()

        app.run(
            host="0.0.0.0",
            port=5000,
            threaded=True,
            use_reloader=False,
        )

    finally:
        cleanup()
