"""
elio_logger.py — MQTT traffic logger for the Elio robot system.

Subscribes to all elio/# topics and prints every message with a timestamp.
Run this on the Raspberry Pi alongside Mosquitto.

Usage:
    python elio_logger.py

Dependencies:
    pip install paho-mqtt
"""

import json
import signal
import sys
from datetime import datetime

import paho.mqtt.client as mqtt

# ---- Config ----
BROKER_HOST = "localhost"
BROKER_PORT = 1883
SUBSCRIBE_TOPIC = "elio/#"
# ----------------

# ANSI colours for topic namespaces
COLOURS = {
    "elio/motor": "\033[36m",  # cyan
    "elio/audio": "\033[35m",  # magenta
    "elio/camera": "\033[33m",  # yellow
    "elio/system": "\033[32m",  # green
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def colour_for(topic: str) -> str:
    for prefix, code in COLOURS.items():
        if topic.startswith(prefix):
            return code
    return ""


def format_payload(raw: bytes) -> str:
    """Pretty-print JSON payloads; fall back to plain string."""
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, separators=(", ", ": "))
    except (json.JSONDecodeError, ValueError):
        return text


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        print(f"[{ts()}] Connected to broker at {BROKER_HOST}:{BROKER_PORT}")
        client.subscribe(SUBSCRIBE_TOPIC)
        print(f"[{ts()}] Subscribed to {SUBSCRIBE_TOPIC!r}\n")
    else:
        print(
            f"[{ts()}] Connection failed — reason code {reason_code}", file=sys.stderr
        )


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    print(f"\n[{ts()}] Disconnected (reason code {reason_code})")


def on_message(client, userdata, msg):
    col = colour_for(msg.topic)
    stamp = ts()
    topic = msg.topic
    payload = format_payload(msg.payload)

    print(f"{DIM}[{stamp}]{RESET} {col}{BOLD}{topic}{RESET}  {payload}")


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # Publish an OFFLINE will so the broker announces if this logger drops
    client.will_set("elio/system/logger", payload="OFFLINE", retain=True)

    try:
        client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    except ConnectionRefusedError:
        print(f"[{ts()}] Could not connect to Mosquitto at {BROKER_HOST}:{BROKER_PORT}")
        print("       Is Mosquitto running? Try: sudo systemctl status mosquitto")
        sys.exit(1)

    client.publish("elio/system/logger", payload="ONLINE", retain=True)

    # Graceful Ctrl+C shutdown
    def handle_sigint(sig, frame):
        print(f"\n[{ts()}] Shutting down logger...")
        client.publish("elio/system/logger", payload="OFFLINE", retain=True)
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    print(f"Elio MQTT Logger — listening on {BROKER_HOST}:{BROKER_PORT}")
    print("Press Ctrl+C to stop.\n")
    client.loop_forever()


if __name__ == "__main__":
    main()
