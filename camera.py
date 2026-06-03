import random
import time

import cv2
import requests
from picamera2 import Picamera2

ESP32_IP = "http://10.140.180.161"


face = cv2.CascadeClassifier(
    "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
)

cam = Picamera2()
cam.configure(cam.create_preview_configuration(main={"size": (320, 240)}))
cam.start()
time.sleep(2)

last_cmd = ""


def send(path):
    try:
        requests.get(ESP32_IP + path, timeout=1)
    except Exception as e:
        print("ESP32 error:", e)


def get_data():
    try:
        r = requests.get(ESP32_IP + "/data", timeout=1)
        return r.json()
    except:
        return None


def send_face_cmd(cmd):
    global last_cmd
    if cmd != last_cmd:
        send("/picmd?move=" + cmd)
        print("Face cmd:", cmd)
        last_cmd = cmd


def detect_face(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    faces = face.detectMultiScale(gray, 1.1, 5)

    if len(faces) == 0:
        return "STOP"

    # Any face detected = tell ESP32 to stop rotating and move forward
    return "FORWARD"


def detect_gesture(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)

    mask = cv2.inRange(hsv, (0, 30, 60), (25, 180, 255))
    mask = cv2.GaussianBlur(mask, (5, 5), 0)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        return "NONE"

    cnt = max(contours, key=cv2.contourArea)

    if cv2.contourArea(cnt) < 2500:
        return "NONE"

    hull = cv2.convexHull(cnt, returnPoints=False)

    if hull is None or len(hull) < 3:
        return "NONE"

    defects = cv2.convexityDefects(cnt, hull)

    fingers = 0

    if defects is not None:
        for i in range(defects.shape[0]):
            s, e, f, d = defects[i, 0]
            if d > 8000:
                fingers += 1

    if fingers <= 1:
        return "ROCK"
    elif fingers == 2:
        return "SCISSORS"
    else:
        return "PAPER"


def decide_result(player, robot):
    if player == robot:
        return "DRAW"

    if player == "ROCK" and robot == "SCISSORS":
        return "LOSE"
    if player == "PAPER" and robot == "ROCK":
        return "LOSE"
    if player == "SCISSORS" and robot == "PAPER":
        return "LOSE"

    return "WIN"


print("Pi AI started - face mode sends only FORWARD or STOP")

while True:
    data = get_data()

    if data is None:
        print("ESP32 not connected")
        time.sleep(1)
        continue

    mode = data.get("controlMode", 0)

    frame = cam.capture_array()

    if mode == 1:
        cmd = detect_face(frame)
        send_face_cmd(cmd)

    elif mode == 3:
        player = detect_gesture(frame)
        print("Game hand:", player)

        if player != "NONE":
            robot = random.choice(["ROCK", "PAPER", "SCISSORS"])
            result = decide_result(player, robot)

            print("Player:", player, "Robot:", robot, "Result:", result)

            send("/game?result=" + result)
            time.sleep(3)

    else:
        last_cmd = ""
        time.sleep(0.5)

    time.sleep(0.2)
