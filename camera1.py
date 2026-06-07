from picamera2 import Picamera2
import cv2
import time
import json
import socket
import sys
import paho.mqtt.client as mqtt
from zeroconf import ServiceInfo, Zeroconf

# ================= HOST CONNECTIVITY CHECK =================
ESP32_HOST = "luna-motor-esp32.local"
ESP32_PORT = 1883
ESP32_TIMEOUT = 5  # seconds

def check_esp32_connection(host: str, port: int, timeout: int) -> None:
    """
    Resolve and TCP-ping the ESP32 before anything else starts.
    Exits with a non-zero status code if the host is unreachable.
    """
    print(f"[startup] Checking connection to {host}:{port} ...")
    try:
        # getaddrinfo resolves .local via mDNS (requires avahi/bonjour on the Pi)
        results = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if not results:
            raise OSError(f"Could not resolve {host}")
        ip = results[0][4][0]
        print(f"[startup] Resolved {host} -> {ip}")

        # Attempt a real TCP connection to confirm the port is open
        with socket.create_connection((ip, port), timeout=timeout):
            pass

        print(f"[startup] ✓ Connected to {host} ({ip}:{port}) — continuing startup.\n")

    except OSError as exc:
        print(f"[startup] ✗ Cannot reach {host}:{port} — {exc}")
        print("[startup] Make sure luna-motor-esp32 is powered on and on the same network.")
        sys.exit(1)

check_esp32_connection(ESP32_HOST, ESP32_PORT, ESP32_TIMEOUT)

# ================= MQTT =================
# MQTT broker runs on this Raspberry Pi
MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883

TOPIC_ROBOT_CMD = "luna/robot/cmd"
TOPIC_ROBOT_STATUS = "luna/robot/status"
TOPIC_ROBOT_SENSORS = "luna/robot/sensors"

# ================= ZEROCONF =================
ZEROCONF_SERVICE_TYPE = "_mqtt._tcp.local."
ZEROCONF_SERVICE_NAME  = "raspberrypi._mqtt._tcp.local."
ZEROCONF_HOSTNAME      = "raspberrypi.local."

def get_local_ip():
    """Return the primary non-loopback IPv4 address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def start_zeroconf():
    local_ip = get_local_ip()
    packed_ip = socket.inet_aton(local_ip)

    info = ServiceInfo(
        type_=ZEROCONF_SERVICE_TYPE,
        name=ZEROCONF_SERVICE_NAME,
        addresses=[packed_ip],
        port=MQTT_PORT,
        properties={
            "hostname": "raspberrypi",
            "version": "1.0",
            "service": "luna-pi-face",
        },
        server=ZEROCONF_HOSTNAME,
    )

    zc = Zeroconf()
    zc.register_service(info)
    print(f"Zeroconf: announced as raspberrypi.local ({local_ip}:{MQTT_PORT})")
    return zc, info

zeroconf, zeroconf_info = start_zeroconf()

# ================= FACE DETECTION =================
face = cv2.CascadeClassifier(
    "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
)

cam = Picamera2()
cam.configure(cam.create_preview_configuration(main={"size": (320, 240)}))
cam.start()
time.sleep(2)

last_cmd = ""
last_status = {}
last_sensors = {}

# ================= MQTT CALLBACKS =================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("MQTT connected")
        client.subscribe(TOPIC_ROBOT_STATUS)
        client.subscribe(TOPIC_ROBOT_SENSORS)

        # Put robot into face follow mode
        client.publish(TOPIC_ROBOT_CMD, "MODE:1")
        client.publish(TOPIC_ROBOT_CMD, "STOP")
    else:
        print("MQTT connect failed:", rc)

def on_message(client, userdata, msg):
    global last_status, last_sensors

    payload = msg.payload.decode(errors="ignore")

    try:
        data = json.loads(payload)
    except:
        data = payload

    if msg.topic == TOPIC_ROBOT_STATUS:
        last_status = data
        print("STATUS:", data)

    elif msg.topic == TOPIC_ROBOT_SENSORS:
        last_sensors = data

# ================= MQTT START =================
client = mqtt.Client(client_id="luna-pi-face")
client.on_connect = on_connect
client.on_message = on_message

client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.loop_start()

# ================= ROBOT COMMAND =================
def send_face_cmd(cmd):
    global last_cmd

    if cmd != last_cmd:
        client.publish(TOPIC_ROBOT_CMD, cmd)
        print("Face cmd:", cmd)
        last_cmd = cmd

def detect_face(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    faces = face.detectMultiScale(gray, 1.1, 5)

    if len(faces) == 0:
        return "STOP"

    # Any face detected = tell ESP32 to stop rotating and move forward
    return "FORWARD"

print("Pi AI started with MQTT")
print("Face detected  -> MQTT luna/robot/cmd = FORWARD")
print("No face        -> MQTT luna/robot/cmd = STOP")

try:
    while True:
        frame = cam.capture_array()

        cmd = detect_face(frame)
        send_face_cmd(cmd)

        time.sleep(0.2)

except KeyboardInterrupt:
    print("\nStopping...")
    client.publish(TOPIC_ROBOT_CMD, "STOP")
    time.sleep(0.2)
    client.loop_stop()
    client.disconnect()
    cam.stop()
    zeroconf.unregister_service(zeroconf_info)
    zeroconf.close()
    print("Stopped")