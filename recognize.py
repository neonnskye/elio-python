import platform
import threading
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template, request

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

# Persistent face database path
FACES_DB = Path("faces_db.npz")


class FacialRecognition:
    def __init__(self):
        self.HOST_OS = platform.system()
        self.latest_frame = None
        self._lock = threading.Lock()
        self._cap = cv2.VideoCapture(3)

        # --- YuNet detector ---
        self._detector = cv2.FaceDetectorYN.create(
            YUNET_MODEL,
            "",
            (320, 320),
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
        Capture the latest webcam frame, find the largest face,
        and add its embedding to known_faces under `name`.
        Returns (success, message).
        """
        with self._lock:
            frame = self.latest_frame.copy() if self.latest_frame is not None else None

        if frame is None:
            return False, "No frame available from camera."

        h, w = frame.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(frame)

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
        self._save_db()

        count = len(self._known_faces[key]["embeddings"])
        print(f"Enrolled: {display!r} (now has {count} photo(s))")
        return True, f"Enrolled {display!r} ({count} photo(s) total)."

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_embedding(self, frame: np.ndarray) -> np.ndarray | None:
        """Detect first face in frame and return its SFace embedding."""
        h, w = frame.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(frame)
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

    def _annotate(self, frame: np.ndarray, faces) -> np.ndarray:
        """Draw bounding boxes and identity labels on frame.

        Reuses cached labels from the previous frame for faces whose bbox
        center hasn't moved more than MATCH_DIST_PX — only "new" faces
        (by position) pay the full SFace feature() cost.
        """
        if faces is None:
            self._tracked_faces = []
            return frame

        new_tracked: list[tuple[float, float, str]] = []
        used: set[int] = set()

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
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            cv2.putText(
                frame, label, (x, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2
            )

        self._tracked_faces = new_tracked
        return frame

    # ------------------------------------------------------------------
    # Main capture loop
    # ------------------------------------------------------------------
    def read_cam(self):
        if not self._cap.isOpened():
            print("Error: Could not open webcam.")
            return

        while True:
            ret, frame = self._cap.read()
            if not ret:
                break

            h, w = frame.shape[:2]
            self._detector.setInputSize((w, h))
            _, faces = self._detector.detect(frame)
            annotated = self._annotate(frame, faces)

            with self._lock:
                self.latest_frame = annotated

    def generate_mjpeg(self):
        while True:
            with self._lock:
                if self.latest_frame is None:
                    continue
                _, buf = cv2.imencode(".jpg", self.latest_frame)
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
            )


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
