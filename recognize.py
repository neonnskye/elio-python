import platform
import threading

import cv2
import numpy as np
from flask import Flask, Response, render_template

app = Flask(__name__)

# Download these from the OpenCV zoo:
# https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
# https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface
YUNET_MODEL = "models/face_detection_yunet_2023mar.onnx"
SFACE_MODEL = "models/face_recognition_sface_2021dec.onnx"

COSINE_THRESHOLD = 0.363  # below this → different person
L2_THRESHOLD = 1.128  # above this → different person (alternative metric)


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
            (320, 320),  # input size; updated dynamically per frame
            score_threshold=0.6,
            nms_threshold=0.3,
            top_k=5000,
        )

        # --- SFace recognizer ---
        self._recognizer = cv2.FaceRecognizerSF.create(SFACE_MODEL, "")

        # known_faces: { "Name": np.ndarray (128-d embedding) }
        self._known_faces: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------
    # Public: enroll a person from a single image file
    # ------------------------------------------------------------------
    def enroll_from_file(self, name: str, image_path: str):
        img = cv2.imread(image_path)
        embedding = self._get_embedding(img)
        if embedding is not None:
            self._known_faces[name] = embedding
            print(f"Enrolled: {name}")
        else:
            print(f"No face found in {image_path}")

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
        # Use the highest-confidence face (first after NMS)
        aligned = self._recognizer.alignCrop(frame, faces[0])
        return self._recognizer.feature(aligned)

    def _identify(self, embedding: np.ndarray) -> str:
        """Cosine-match embedding against enrolled faces."""
        best_name = "Unknown"
        best_score = -1.0
        for name, known_emb in self._known_faces.items():
            score = self._recognizer.match(
                embedding, known_emb, cv2.FaceRecognizerSF_FR_COSINE
            )
            if score > best_score:
                best_score = score
                best_name = name
        if best_score < COSINE_THRESHOLD:
            return "Unknown"
        return f"{best_name} ({best_score:.2f})"

    def _annotate(self, frame: np.ndarray, faces) -> np.ndarray:
        """Draw bounding boxes and identity labels on frame."""
        if faces is None:
            return frame
        for face in faces:
            x, y, w, h = (int(v) for v in face[:4])
            aligned = self._recognizer.alignCrop(frame, face)
            emb = self._recognizer.feature(aligned)
            label = self._identify(emb)

            color = (0, 255, 0) if not label.startswith("Unknown") else (0, 0, 255)
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            cv2.putText(
                frame, label, (x, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2
            )
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

# Enroll known faces before starting — add as many as you need
# recognition.enroll_from_file("Amrith", "amrith.jpg")


@app.route("/stream")
def stream():
    return Response(
        recognition.generate_mjpeg(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    cam_thread = threading.Thread(target=recognition.read_cam, daemon=True)
    cam_thread.start()
    app.run(host="0.0.0.0", port=5000, use_reloader=False)
