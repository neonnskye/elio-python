import platform
import threading

import cv2
from flask import Flask, Response

app = Flask(__name__)


class FacialRecognition:
    def __init__(self):
        self.HOST_OS = platform.system()
        self.latest_frame = None
        self._lock = threading.Lock()
        self._cap = cv2.VideoCapture(3)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

    def read_cam(self):
        if not self._cap.isOpened():
            print("Error: Could not open webcam.")
            return

        while True:
            ret, frame = self._cap.read()
            if not ret:
                break
            with self._lock:
                self.latest_frame = frame.copy()

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


@app.route("/")
def index():
    return """
    <html>
    <body style="margin:0;background:#000">
        <img src="/stream" style="width:100%;height:100vh;object-fit:contain">
    </body>
    </html>
    """


if __name__ == "__main__":
    cam_thread = threading.Thread(target=recognition.read_cam, daemon=True)
    cam_thread.start()

    app.run(host="0.0.0.0", port=5000, use_reloader=False)
