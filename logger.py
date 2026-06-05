"""
elio_logger.py — Minimal MQTT logger.

Connects to localhost, subscribes to all topics, and prints each message with a timestamp.

Usage:
    python elio_logger.py

Dependencies:
    pip install paho-mqtt
"""

import signal
import sys
from datetime import datetime

import paho.mqtt.client as mqtt

BROKER_HOST = "localhost"
BROKER_PORT = 1883


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        client.subscribe("#")
        print(f"[{ts()}] Connected — subscribed to all topics\n")
    else:
        print(
            f"[{ts()}] Connection failed (reason code {reason_code})", file=sys.stderr
        )


def on_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8", errors="replace").strip()
    print(f"[{ts()}] {msg.topic}  {payload}")


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    except ConnectionRefusedError:
        print(f"[{ts()}] Could not connect to broker at {BROKER_HOST}:{BROKER_PORT}")
        print("       Is Mosquitto running? Try: sudo systemctl status mosquitto")
        sys.exit(1)

    def handle_sigint(sig, frame):
        print(f"\n[{ts()}] Stopped.")
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    print(f"MQTT Logger — {BROKER_HOST}:{BROKER_PORT}  |  Press Ctrl+C to stop.\n")
    client.loop_forever()


if __name__ == "__main__":
    main()
