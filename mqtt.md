## Full MQTT Topic Specification — Elio System

### Namespace conventions
- Root: `elio/`
- Direction: `cmd` = something is being commanded, `state` = something is reporting what it is, `event` = something happened (momentary, not continuous state)
- Payloads are plain strings or compact JSON. Plain strings are used where the value is a single scalar; JSON only where multiple fields are needed together.

---

### `elio/motor/` — Motor ESP32

**Subscribes to (commands in):**

| Topic | Payload | Notes |
|---|---|---|
| `elio/motor/cmd/mode` | `"1"` / `"2"` / `"3"` | 1=Face Follow, 2=Manual, 3=Mini Game |
| `elio/motor/cmd/move` | `"FORWARD"` / `"BACKWARD"` / `"LEFT"` / `"RIGHT"` / `"STOP"` | Used in manual mode and by voice |
| `elio/motor/cmd/speed` | `"0"`–`"255"` | PWM speed |
| `elio/motor/cmd/dance` | `"0"`–`"3"` | 0=off, 1/2/3=slow/medium/fast |
| `elio/motor/cmd/picmd` | `"FORWARD"` / `"STOP"` | Camera Pi publishes here; replaces `GET /picmd` |
| `elio/motor/cmd/game` | `"WIN"` / `"LOSE"` / `"DRAW"` | Camera Pi publishes here after gesture result |

**Publishes (state out):**

| Topic | Payload | Rate | Notes |
|---|---|---|---|
| `elio/motor/state` | JSON (see below) | Every 500ms | Full telemetry snapshot |
| `elio/motor/event/edge` | `"DETECTED"` / `"CLEAR"` | On change only | Safety-critical, triggers fast |
| `elio/motor/event/obstacle` | `"NEAR"` / `"CLEAR"` | On change only | Front/left/right collapse to one signal |
| `elio/motor/event/mode` | `"1"` / `"2"` / `"3"` | On change only | So audio pipeline knows current mode |

**`elio/motor/state` JSON:**
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

---

### `elio/audio/` — Audio ESP32 + receiver.py

The audio ESP32 currently speaks binary UDP to `receiver.py`. That channel (raw audio stream + control bytes) should **stay as UDP** — MQTT is the wrong transport for real-time audio frames. What MQTT adds here is signalling around the pipeline state, which everything else needs to react to.

**Audio ESP32 publishes:**

| Topic | Payload | Notes |
|---|---|---|
| `elio/audio/event/wakeword` | `"DETECTED"` | Fires when Edge Impulse score > 0.6; replaces the UDP `0x01` control byte as the cross-system signal |

**receiver.py publishes:**

| Topic | Payload | Notes |
|---|---|---|
| `elio/audio/state` | JSON (see below) | On every state transition |
| `elio/audio/event/transcript` | `"<raw text>"` | The STT result, as soon as Groq returns it |
| `elio/audio/event/intent` | JSON (see below) | After LLM parses a command — this is what drives motors |
| `elio/audio/event/response` | `"<plain text>"` | The full LLM response text (useful for logging/display) |

**`elio/audio/state` JSON:**
```json
{
  "state": "TRANSCRIBING"
}
```
States map directly to your `ListenState` enum: `IDLE`, `SKIP_WAKEWORD_BLEED`, `CAPTURING`, `TRANSCRIBING`, `RESPONDING`.

**`elio/audio/event/intent` JSON:**
```json
{
  "action": "move",
  "value": "FORWARD"
}
```
Other action examples: `{"action": "set_mode", "value": "2"}`, `{"action": "dance", "value": "1"}`, `{"action": "set_speed", "value": "180"}`. This is the key integration topic — the orchestrator subscribes here and translates to `elio/motor/cmd/*` publishes.

**receiver.py subscribes to:**

| Topic | Payload | Notes |
|---|---|---|
| `elio/audio/cmd/speak` | `"<text>"` | External trigger for TTS — lets other systems make Elio speak |

---

### `elio/camera/` — camera.py (Raspberry Pi)

**Publishes:**

| Topic | Payload | Notes |
|---|---|---|
| `elio/camera/event/face` | `"DETECTED"` / `"LOST"` | On change only |
| `elio/camera/event/gesture` | `"ROCK"` / `"PAPER"` / `"SCISSORS"` / `"NONE"` | When gesture detected in mode 3 |
| `elio/camera/event/game_result` | `"WIN"` / `"LOSE"` / `"DRAW"` | After decide_result(); replaces `GET /game` |

**Subscribes to:**

| Topic | Notes |
|---|---|
| `elio/motor/state` | Replaces the `/data` poll loop; reads `controlMode` from this |

---

### `elio/system/` — Orchestrator / broker (your new Python script)

**Publishes:**

| Topic | Payload | Notes |
|---|---|---|
| `elio/system/log` | JSON `{"ts": "...", "level": "INFO", "msg": "..."}` | All events mirror here for your logger |
| `elio/system/status` | `"ONLINE"` with `retain=True` | Heartbeat; set `will` to `"OFFLINE"` so broker publishes it on disconnect |