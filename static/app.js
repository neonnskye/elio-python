const btn = document.getElementById("enroll-btn");
const input = document.getElementById("name-input");
const status = document.getElementById("status-msg");

async function enroll() {
  const name = input.value.trim();
  if (!name) {
    showStatus("Enter a name first.", false);
    return;
  }

  btn.disabled = true;
  btn.textContent = "Enrolling…";
  status.textContent = "";

  try {
    const res = await fetch("/enroll", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await res.json();
    showStatus(data.message, data.ok);
    if (data.ok) input.value = "";
  } catch (err) {
    showStatus("Request failed.", false);
  } finally {
    btn.disabled = false;
    btn.textContent = "Enroll";
  }
}

function showStatus(msg, ok) {
  status.textContent = msg;
  status.className = ok ? "ok" : "err";
}

btn.addEventListener("click", enroll);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter") enroll();
});

const stopBtn = document.getElementById("stop-btn");

async function stopSpeaking() {
  stopBtn.disabled = true;
  stopBtn.classList.add("stopped");

  try {
    await fetch("/stop", { method: "POST" });
  } finally {
    setTimeout(() => {
      stopBtn.disabled = false;
    }, 900);
  }
}

stopBtn.addEventListener("click", stopSpeaking);

const listenToggleBtn = document.getElementById("listen-toggle-btn");
const LISTEN_LABEL_ON = "Elio"; // currently listening; click to mute
const LISTEN_LABEL_MUTED = "Elio"; // currently muted; click to resume

async function toggleListening() {
  listenToggleBtn.disabled = true;

  try {
    const res = await fetch("/toggle-listen", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      applyListenState(data.muted);
    }
  } finally {
    listenToggleBtn.disabled = false;
  }
}

function applyListenState(muted) {
  listenToggleBtn.classList.toggle("muted", muted);
  listenToggleBtn.textContent = muted ? LISTEN_LABEL_MUTED : LISTEN_LABEL_ON;
}

listenToggleBtn.addEventListener("click", toggleListening);
