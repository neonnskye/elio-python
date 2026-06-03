# Elio System — MQTT Integration Guide
### For the Motor/Camera Team

---

## Overview

We are migrating from direct HTTP/Wi-Fi communication to an MQTT pub/sub architecture. All components — the motor ESP32, the camera Pi script, and the audio system — will communicate through a central MQTT broker running on the Raspberry Pi. This lets every part of the system react to events from every other part without any component needing to know another's IP address or poll an HTTP endpoint.

For now, the orchestrator (`orchestrator.py`) is logging all traffic only. It does not act on any messages yet. Integration logic (e.g. routing voice commands to the motors) will be wired in after everyone is publishing and subscribing correctly.

Your job right now: replace all HTTP calls in `elio-motor.ino` and `camera.py` with MQTT pub/sub.

---

## Broker Connection

The Mosquitto MQTT broker runs on the Raspberry Pi. **Connect to it by IP address** — do not use `localhost` unless you are running code on the Pi itself.

```
Host: <Raspberry Pi LAN IP>   (e.g. 10.140.180.x — confirm with your team)
Port: 1883
```

There is no username/password configured at this stage. If Mosquitto refuses connections from external clients, check that `listener 1883` and `allow_anonymous true` are set in `/etc/mosquitto/mosquitto.conf` on the Pi and that the service has been restarted (`sudo systemctl restart mosquitto`).

---

## Topic Reference

All topics live under the `elio/` namespace. The three sub-namespaces relevant to you are:

```
elio/motor/   — commands to the motor ESP32, state published by it
elio/camera/  — events published by camera.py
elio/system/  — broker-level status (logger only, ignore for now)
```

The audio system operates under `elio/audio/` — that namespace is managed separately and you do not need to publish or subscribe to it.

### Topics you need to subscribe to (incoming commands)

| Topic | Who subscribes | Payload | What to do with it |
|---|---|---|---|
| `elio/motor/cmd/mode` | Motor ESP32 | `"1"` / `"2"` / `"3"` | Set `controlMode` (1=Face Follow, 2=Manual, 3=Mini Game) |
| `elio/motor/cmd/move` | Motor ESP32 | `"FORWARD"` / `"BACKWARD"` / `"LEFT"` / `"RIGHT"` / `"STOP"` | Execute movement (used in manual mode and eventually by voice) |
| `elio/motor/cmd/speed` | Motor ESP32 | `"0"` – `"255"` | Set PWM speed |
| `elio/motor/cmd/dance` | Motor ESP32 | `"0"` – `"3"` | Set dance mode |
| `elio/motor/cmd/picmd` | Motor ESP32 | `"FORWARD"` / `"STOP"` | Camera Pi's face-follow command — replaces `GET /picmd` |
| `elio/motor/cmd/game` | Motor ESP32 | `"WIN"` / `"LOSE"` / `"DRAW"` | Game result from camera Pi — replaces `GET /game` |
| `elio/motor/state` | camera.py | JSON (see below) | Read `controlMode` field — replaces the `GET /data` poll loop |

### Topics you need to publish (outgoing state/events)

| Topic | Who publishes | Payload | When |
|---|---|---|---|
| `elio/motor/state` | Motor ESP32 | JSON snapshot (see below) | Every 500 ms |
| `elio/motor/event/edge` | Motor ESP32 | `"DETECTED"` / `"CLEAR"` | On change only |
| `elio/motor/event/obstacle` | Motor ESP32 | `"NEAR"` / `"CLEAR"` | On change only |
| `elio/motor/event/mode` | Motor ESP32 | `"1"` / `"2"` / `"3"` | On mode change |
| `elio/camera/event/face` | camera.py | `"DETECTED"` / `"LOST"` | When face presence changes |
| `elio/camera/event/gesture` | camera.py | `"ROCK"` / `"PAPER"` / `"SCISSORS"` / `"NONE"` | When a gesture is read in mode 3 |
| `elio/camera/event/game_result` | camera.py | `"WIN"` / `"LOSE"` / `"DRAW"` | After `decide_result()` — replaces `GET /game` |

### `elio/motor/state` JSON payload

```json
{
  "state": "FORWARD",
  "status": "FACE CENTER - FORWARD",
  "controlMode": 1,
  "dance": 0,
  "speed": 240,
  "front": 42,
  "left": 80,
  "right": 75,
  "edge": false,
  "face": "FACE DETECTED",
  "gameStatus": "NO GAME",
  "gameResult": "NONE"
}
```

This replaces the `/data` endpoint. `camera.py` should subscribe to this topic instead of polling `GET /data`.

---

## What Changes in Each File

### `elio-motor.ino` — Replace the entire WebServer with MQTT

Currently the motor ESP32 runs an HTTP web server and exposes endpoints (`/data`, `/mode`, `/manual`, `/picmd`, `/game`, `/dance`, `/speed`). Replace all of this with MQTT.

**Remove:**
- `#include <WebServer.h>` and the `WebServer server(80)` instance
- All `server.on(...)` route registrations
- `server.begin()` and `server.handleClient()` in `loop()`
- The HTML page and `htmlPage()` function (optionally keep as a debug reference)
- `handlePiCmd()`, `handleGame()`, `handleMode()`, `handleManual()`, `handleDance()`, `handleSpeed()`, `handleData()`

**Add:**
- `#include <PubSubClient.h>` (see library setup below)
- MQTT client setup, connection, subscription, and a 500 ms state publish

The web dashboard will stop working once you remove the server. If you want to keep it for testing, keep `WebServer` running in parallel during the transition and remove it only once MQTT is verified.

### `camera.py` — Replace requests with paho-mqtt

Currently `camera.py` uses `requests.get(ESP32_IP + "/picmd?move=...")` and `requests.get(ESP32_IP + "/game?result=...")` to send commands, and `requests.get(ESP32_IP + "/data")` to read state.

**Remove:**
- The `requests` import
- `ESP32_IP` constant
- `send()`, `get_data()`, `send_face_cmd()` helper functions
- The `data = get_data()` poll at the top of the main loop
- `send("/picmd?move=" + cmd)` call
- `send("/game?result=" + result)` call

**Add:**
- `paho-mqtt` client
- Subscribe to `elio/motor/state` to read `controlMode`
- Publish `elio/motor/cmd/picmd` instead of `GET /picmd`
- Publish `elio/motor/cmd/game` and `elio/camera/event/game_result` instead of `GET /game`
- Publish `elio/camera/event/face` when face presence changes

---

## Implementation: Python (`camera.py`)

Install the library:

```bash
pip install paho-mqtt
```

### Boilerplate — connect, subscribe, publish

```python
import paho.mqtt.client as mqtt
import json
import time

BROKER_IP = "10.140.180.x"   # replace with actual Pi IP
BROKER_PORT = 1883

current_mode = 1              # updated from elio/motor/state subscription

def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        print("Connected to MQTT broker")
        client.subscribe("elio/motor/state")
    else:
        print("Failed to connect, reason code:", reason_code)

def on_message(client, userdata, msg):
    global current_mode
    if msg.topic == "elio/motor/state":
        try:
            data = json.loads(msg.payload.decode())
            current_mode = data.get("controlMode", 1)
        except Exception as e:
            print("Failed to parse motor state:", e)

mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

mqtt_client.connect(BROKER_IP, BROKER_PORT, keepalive=60)
mqtt_client.loop_start()    # runs MQTT in background thread
```

`loop_start()` is important — it keeps the MQTT connection alive and calls callbacks in a background thread so it does not block your main camera loop.

### Publishing commands

```python
# Replace send("/picmd?move=FORWARD")
mqtt_client.publish("elio/motor/cmd/picmd", "FORWARD")

# Replace send("/picmd?move=STOP")
mqtt_client.publish("elio/motor/cmd/picmd", "STOP")

# Replace send("/game?result=" + result)
mqtt_client.publish("elio/motor/cmd/game", result)           # tells motor ESP32 to react
mqtt_client.publish("elio/camera/event/game_result", result) # broadcast for any other subscriber
```

### Publishing face events

Publish `elio/camera/event/face` when face presence changes so the rest of the system can react:

```python
last_face_state = None

def publish_face_event(client, detected: bool):
    global last_face_state
    value = "DETECTED" if detected else "LOST"
    if value != last_face_state:
        client.publish("elio/camera/event/face", value)
        last_face_state = value
```

Call `publish_face_event(mqtt_client, len(faces) > 0)` inside your face detection path.

### Updated main loop structure

```python
while True:
    # current_mode is now updated by the on_message callback, not a poll
    mode = current_mode

    frame = cam.capture_array()

    if mode == 1:
        cmd = detect_face(frame)
        faces_detected = (cmd == "FORWARD")
        publish_face_event(mqtt_client, faces_detected)

        global last_cmd
        if cmd != last_cmd:
            mqtt_client.publish("elio/motor/cmd/picmd", cmd)
            print("Face cmd:", cmd)
            last_cmd = cmd

    elif mode == 3:
        player = detect_gesture(frame)
        print("Game hand:", player)

        if player != "NONE":
            robot = random.choice(["ROCK", "PAPER", "SCISSORS"])
            result = decide_result(player, robot)
            print("Player:", player, "Robot:", robot, "Result:", result)

            mqtt_client.publish("elio/motor/cmd/game", result)
            mqtt_client.publish("elio/camera/event/game_result", result)
            time.sleep(3)

    else:
        last_cmd = ""
        time.sleep(0.5)

    time.sleep(0.2)
```

---

## Implementation: ESP32 (`elio-motor.ino`)

### Library setup

Install `PubSubClient` by Nick O'Leary in the Arduino/PlatformIO library manager. In PlatformIO add to `platformio.ini`:

```ini
lib_deps =
    knolleary/PubSubClient@^2.8
```

### Includes and config

```cpp
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>   // for building the state JSON

const char* BROKER_IP   = "10.140.180.x";  // replace with actual Pi IP
const int   BROKER_PORT = 1883;

WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);
```

`ArduinoJson` is already common in ESP32 projects. Add it to `lib_deps` too:

```ini
lib_deps =
    knolleary/PubSubClient@^2.8
    bblanchon/ArduinoJson@^7
```

### MQTT callback — handle incoming commands

```cpp
void mqttCallback(char* topic, byte* payload, unsigned int length) {
    String t = String(topic);
    String msg = "";
    for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];

    if (t == "elio/motor/cmd/mode") {
        int m = msg.toInt();
        if (m >= 1 && m <= 3) {
            controlMode = m;
            danceMode   = 0;
            manualCommand = "STOP";
            stopRobot();
            if (controlMode == 1) sensorStatus = "FACE FOLLOW MODE";
            else if (controlMode == 2) sensorStatus = "MANUAL MODE";
            else if (controlMode == 3) sensorStatus = "MINI GAME MODE";
            mqtt.publish("elio/motor/event/mode", msg.c_str());
        }

    } else if (t == "elio/motor/cmd/move") {
        manualCommand = msg;
        controlMode   = 2;
        danceMode     = 0;
        sensorStatus  = "MANUAL COMMAND: " + msg;

    } else if (t == "elio/motor/cmd/speed") {
        setSpeed(msg.toInt());

    } else if (t == "elio/motor/cmd/dance") {
        int requestedDance = msg.toInt();
        readSensors();
        if (requestedDance != 0 && !safeForDanceOrFace()) {
            danceMode = 0;
            stopRobot();
            sensorStatus = "CAN'T DANCE - OBSTACLE OR EDGE";
        } else {
            danceMode  = requestedDance;
            danceStep  = 0;
            danceTimer = millis();
            if (danceMode == 0) { noTone(BUZZER_PIN); stopRobot(); sensorStatus = "DANCE STOPPED"; }
            else { sensorStatus = "DANCE MODE STARTED"; }
        }

    } else if (t == "elio/motor/cmd/picmd") {
        piCommand          = msg;
        lastPiCommandTime  = millis();
        controlMode        = 1;
        danceMode          = 0;
        faceStatus         = (piCommand == "STOP") ? "NO FACE" : "FACE DETECTED";
        sensorStatus       = "PI COMMAND: " + msg;

    } else if (t == "elio/motor/cmd/game") {
        gameResult  = msg;
        controlMode = 3;
        danceMode   = 0;

        if (gameResult == "WIN") {
            gameStatus   = "ROBOT WIN";
            sensorStatus = "MINI GAME - ROBOT WIN";
            tone(BUZZER_PIN, 900, 200);
            startAction(turnLeft, "ROBOT WIN DANCE", 350);
        } else if (gameResult == "LOSE") {
            gameStatus   = "ROBOT LOSE";
            sensorStatus = "MINI GAME - ROBOT LOSE";
            tone(BUZZER_PIN, 300, 400);
            startAction(moveBackward, "ROBOT LOSE - BACK", 500);
        } else if (gameResult == "DRAW") {
            gameStatus   = "DRAW";
            sensorStatus = "MINI GAME - DRAW";
            tone(BUZZER_PIN, 600, 150);
            stopRobot();
        } else {
            gameStatus   = "GAME READY";
            sensorStatus = "MINI GAME MODE";
            stopRobot();
        }
    }
}
```

### Connect and subscribe

```cpp
void mqttConnect() {
    while (!mqtt.connected()) {
        Serial.print("Connecting to MQTT...");
        if (mqtt.connect("elio-motor-esp32")) {
            Serial.println(" connected");
            mqtt.subscribe("elio/motor/cmd/mode");
            mqtt.subscribe("elio/motor/cmd/move");
            mqtt.subscribe("elio/motor/cmd/speed");
            mqtt.subscribe("elio/motor/cmd/dance");
            mqtt.subscribe("elio/motor/cmd/picmd");
            mqtt.subscribe("elio/motor/cmd/game");
        } else {
            Serial.print(" failed, rc=");
            Serial.println(mqtt.state());
            delay(2000);
        }
    }
}
```

Call this in `setup()` after WiFi connects:

```cpp
mqtt.setServer(BROKER_IP, BROKER_PORT);
mqtt.setCallback(mqttCallback);
mqttConnect();
```

### Publishing state every 500 ms

Add a timer variable at the top:

```cpp
unsigned long lastMqttPublish = 0;
bool lastEdge     = false;
bool lastObstacle = false;
int  lastMode     = -1;
```

Then add this block to `loop()`, after `readSensors()`:

```cpp
// Publish full state snapshot every 500ms
unsigned long now = millis();
if (now - lastMqttPublish >= 500) {
    lastMqttPublish = now;

    StaticJsonDocument<384> doc;
    doc["state"]       = robotState;
    doc["status"]      = sensorStatus;
    doc["controlMode"] = controlMode;
    doc["dance"]       = danceMode;
    doc["speed"]       = speedValue;
    doc["front"]       = frontDist;
    doc["left"]        = leftDist;
    doc["right"]       = rightDist;
    doc["edge"]        = edgeDetected();
    doc["face"]        = faceStatus;
    doc["gameStatus"]  = gameStatus;
    doc["gameResult"]  = gameResult;

    char buf[384];
    serializeJson(doc, buf, sizeof(buf));
    mqtt.publish("elio/motor/state", buf);
}

// Publish edge event on change
bool edge = edgeDetected();
if (edge != lastEdge) {
    mqtt.publish("elio/motor/event/edge", edge ? "DETECTED" : "CLEAR");
    lastEdge = edge;
}

// Publish obstacle event on change
bool obstacle = obstacleNear();
if (obstacle != lastObstacle) {
    mqtt.publish("elio/motor/event/obstacle", obstacle ? "NEAR" : "CLEAR");
    lastObstacle = obstacle;
}

// Keep MQTT alive and call mqttCallback for incoming messages
if (!mqtt.connected()) mqttConnect();
mqtt.loop();
```

**Important:** `mqtt.loop()` must be called on every iteration of `loop()`. Without it, incoming messages will never be processed.

### Remove the WebServer

Once the above is working, remove:

```cpp
// Remove these:
#include <WebServer.h>
WebServer server(80);

// In setup():
server.on("/", handleRoot);
// ... all server.on() calls ...
server.begin();

// In loop():
server.handleClient();
```

And delete the handler functions (`handleData`, `handleMode`, `handleManual`, `handlePiCmd`, `handleGame`, `handleDance`, `handleSpeed`, `handleRoot`, `htmlPage`).

---

## Testing Workflow

### Step 1 — Verify the broker is running

On the Raspberry Pi:

```bash
sudo systemctl status mosquitto
```

### Step 2 — Run the logger

On the Raspberry Pi, with the virtualenv active:

```bash
python orchestrator.py
```

This subscribes to `elio/#` and prints every message with a timestamp. Leave this running while you test.

### Step 3 — Test from the command line with mosquitto_clients

Install on any machine on the same network:

```bash
sudo apt install mosquitto-clients   # Linux
brew install mosquitto               # macOS
```

Subscribe to all topics in one terminal:

```bash
mosquitto_sub -h <PI_IP> -t "elio/#" -v
```

Publish a test command in another:

```bash
# Send a manual move command
mosquitto_pub -h <PI_IP> -t "elio/motor/cmd/move" -m "FORWARD"

# Change mode
mosquitto_pub -h <PI_IP> -t "elio/motor/cmd/mode" -m "2"

# Trigger a game result
mosquitto_pub -h <PI_IP> -t "elio/motor/cmd/game" -m "WIN"
```

If `orchestrator.py` prints the message and the motor ESP32 reacts, the integration is working.

### Step 4 — Verify camera.py

Run `camera.py` on the Pi. Point the camera at a face and confirm:

- `elio/camera/event/face DETECTED` appears in the logger
- `elio/motor/cmd/picmd FORWARD` appears in the logger
- The motor ESP32 reacts

For game mode, set mode 3 via `mosquitto_pub` and check that gesture results flow through.

---

## Notes and Suggestions

**Client IDs must be unique.** If two devices connect with the same client ID, the broker will disconnect the first one. Use descriptive IDs: `"elio-motor-esp32"`, `"elio-camera-pi"`.

**QoS 0 is fine for now.** The default quality-of-service (fire and forget) is sufficient for this project. Don't add retained messages to command topics — a motor command published before the ESP32 connects should not be replayed on startup.

**The `elio/motor/state` publish replaces the `/data` poll.** The old `camera.py` polled `/data` in a tight loop. With MQTT, `camera.py` simply updates `current_mode` whenever a new state message arrives. The broker pushes it; you don't pull.

**Keep the web dashboard for debugging if you want.** You can run the WebServer and MQTT client at the same time on the ESP32 during the transition. Remove the WebServer only once you are confident MQTT is solid.

**`PubSubClient` default buffer is 256 bytes.** The `elio/motor/state` JSON is larger than that. Add this before `mqtt.setServer(...)` in `setup()`:

```cpp
mqtt.setBufferSize(512);
```

**The orchestrator is logging only right now.** Do not expect it to route anything. When voice commands are ready on the audio side, the orchestrator will be updated to translate `elio/audio/event/intent` into `elio/motor/cmd/*` publishes. You do not need to do anything for that — just make sure your subscriptions are in place and working.