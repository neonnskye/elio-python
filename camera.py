import cv2
import time
import json
import socket
import platform
import signal
import sys
import paho.mqtt.client as mqtt

try:
    from zeroconf import ServiceInfo, Zeroconf
    ZEROCONF_AVAILABLE = True
except Exception:
    ZEROCONF_AVAILABLE = False

CAMERA_MODE = "AUTO"

MQTT_BROKER_OPTIONS = [
    "127.0.0.1",
    "localhost",
    "raspberrypi.local",
    "10.158.207.160"
]

MQTT_PORT = 1883

TOPIC_ROBOT_CMD = "luna/robot/cmd"
TOPIC_ROBOT_STATUS = "luna/robot/status"
TOPIC_ROBOT_SENSORS = "luna/robot/sensors"

CAMERA_WIDTH = 320
CAMERA_HEIGHT = 240
CAMERA_INDEX = 0

last_cmd = ""
last_status = {}
last_sensors = {}
client = None
camera = None
zeroconf = None
zeroconf_info = None


def get_haarcascade_path():
    p1 = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    p2 = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"

    try:
        open(p1, "r").close()
        return p1
    except Exception:
        pass

    try:
        open(p2, "r").close()
        return p2
    except Exception:
        pass

    raise FileNotFoundError("Cannot find haarcascade_frontalface_default.xml")


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_zeroconf():
    if not ZEROCONF_AVAILABLE:
        print("Zeroconf not installed. mDNS skipped.")
        return None, None

    local_ip = get_local_ip()
    packed_ip = socket.inet_aton(local_ip)

    info = ServiceInfo(
        type_="_mqtt._tcp.local.",
        name="raspberrypi._mqtt._tcp.local.",
        addresses=[packed_ip],
        port=MQTT_PORT,
        properties={
            "hostname": "raspberrypi",
            "project": "Prometheus-LUNA",
            "service": "mqtt-broker",
        },
        server="raspberrypi.local.",
    )

    zc = Zeroconf()
    zc.register_service(info)

    print(f"mDNS advertised: raspberrypi.local -> {local_ip}:{MQTT_PORT}")
    return zc, info


def is_raspberry_pi():
    if platform.system().lower() != "linux":
        return False

    try:
        with open("/proc/device-tree/model", "r") as f:
            model = f.read().lower()
            return "raspberry pi" in model
    except Exception:
        return False


def choose_camera_mode():
    if CAMERA_MODE.upper() == "WINDOWS":
        return "WINDOWS"

    if CAMERA_MODE.upper() == "PI":
        return "PI"

    if is_raspberry_pi():
        return "PI"

    return "WINDOWS"


class WindowsOpenCVCamera:
    def __init__(self, index=0):
        self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)

        if not self.cap.isOpened():
            self.cap = cv2.VideoCapture(index)

        if not self.cap.isOpened():
            raise RuntimeError("Cannot open laptop webcam")

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)

    def capture_array(self):
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("Cannot read frame from webcam")
        return frame

    def stop(self):
        self.cap.release()


class PiCamera2Wrapper:
    def __init__(self):
        from picamera2 import Picamera2

        self.cam = Picamera2()
        self.cam.configure(
            self.cam.create_preview_configuration(
                main={"size": (CAMERA_WIDTH, CAMERA_HEIGHT)}
            )
        )
        self.cam.start()
        time.sleep(2)

    def capture_array(self):
        return self.cam.capture_array()

    def stop(self):
        self.cam.stop()


def start_camera():
    mode = choose_camera_mode()
    print(f"Camera mode: {mode}")

    if mode == "PI":
        return PiCamera2Wrapper(), "RGB"

    return WindowsOpenCVCamera(CAMERA_INDEX), "BGR"


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("MQTT connected")

        client.subscribe(TOPIC_ROBOT_STATUS)
        client.subscribe(TOPIC_ROBOT_SENSORS)

        client.publish(TOPIC_ROBOT_CMD, "MODE:1")
        time.sleep(0.1)
        client.publish(TOPIC_ROBOT_CMD, "STOP")

    else:
        print("MQTT connect failed:", rc)


def on_message(client, userdata, msg):
    global last_status, last_sensors

    payload = msg.payload.decode(errors="ignore")

    try:
        data = json.loads(payload)
    except Exception:
        data = payload

    if msg.topic == TOPIC_ROBOT_STATUS:
        last_status = data
        print("STATUS:", data)

    elif msg.topic == TOPIC_ROBOT_SENSORS:
        last_sensors = data


def start_mqtt():
    mqtt_client = mqtt.Client(client_id="luna-face-camera")
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    for broker in MQTT_BROKER_OPTIONS:
        try:
            print(f"Trying MQTT broker: {broker}:{MQTT_PORT}")
            mqtt_client.connect(broker, MQTT_PORT, 60)
            mqtt_client.loop_start()

            print(f"Connected to MQTT broker: {broker}")
            return mqtt_client

        except Exception as e:
            print(f"Failed: {broker} -> {e}")

    raise RuntimeError("Could not connect to any MQTT broker")


def send_face_cmd(cmd):
    global last_cmd

    if cmd != last_cmd:
        client.publish(TOPIC_ROBOT_CMD, cmd)
        print("Face cmd:", cmd)
        last_cmd = cmd


def detect_face(frame, color_mode, face_detector):
    if color_mode == "RGB":
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    else:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    faces = face_detector.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(40, 40)
    )

    if len(faces) == 0:
        return "STOP"

    return "FORWARD"


def cleanup():
    global client, camera, zeroconf, zeroconf_info

    print("\nStopping...")

    try:
        if client:
            client.publish(TOPIC_ROBOT_CMD, "STOP")
            time.sleep(0.2)
            client.loop_stop()
            client.disconnect()
    except Exception:
        pass

    try:
        if camera:
            camera.stop()
    except Exception:
        pass

    try:
        if zeroconf and zeroconf_info:
            zeroconf.unregister_service(zeroconf_info)
            zeroconf.close()
    except Exception:
        pass

    print("Stopped")


def signal_handler(sig, frame):
    cleanup()
    sys.exit(0)


def main():
    global client, camera, zeroconf, zeroconf_info

    signal.signal(signal.SIGINT, signal_handler)

    print("Prometheus/LUNA face MQTT started")
    print("Face detected -> FORWARD")
    print("No face       -> STOP")
    print("No camera live preview enabled")

    zeroconf, zeroconf_info = start_zeroconf()

    client = start_mqtt()

    cascade_path = get_haarcascade_path()
    print("Using haarcascade:", cascade_path)

    face_detector = cv2.CascadeClassifier(cascade_path)

    if face_detector.empty():
        raise RuntimeError("Failed to load face cascade")

    camera, color_mode = start_camera()

    print("Running. Press Ctrl+C to stop.")

    try:
        while True:
            frame = camera.capture_array()

            cmd = detect_face(frame, color_mode, face_detector)
            send_face_cmd(cmd)

            time.sleep(0.2)

    finally:
        cleanup()


if __name__ == "__main__":
    main()