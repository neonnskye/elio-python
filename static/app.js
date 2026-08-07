const btn = document.getElementById("enroll-btn");
const input = document.getElementById("name-input");
const status = document.getElementById("status-msg");
const muteBtn = document.getElementById("mute-btn");

// ---- Enroll ----
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

// ---- Mute Speaker ----
let isMuted = false;

function applyMuteState(muted) {
  isMuted = muted;
  const icon = muteBtn.querySelector(".mute-icon");
  const label = muteBtn.querySelector(".mute-label");
  if (muted) {
    muteBtn.classList.remove("unmuted");
    muteBtn.classList.add("muted");
    icon.textContent = "🔇";
    label.textContent = "Unmute Speaker";
    muteBtn.setAttribute("aria-label", "Unmute speaker");
  } else {
    muteBtn.classList.remove("muted");
    muteBtn.classList.add("unmuted");
    icon.textContent = "🔊";
    label.textContent = "Mute Speaker";
    muteBtn.setAttribute("aria-label", "Mute speaker");
  }
}

// Sync button state with server on page load
(async () => {
  try {
    const res = await fetch("/mute-status");
    const data = await res.json();
    applyMuteState(data.muted);
  } catch (_) {
    /* server not ready yet — default to unmuted */
  }
})();

muteBtn.addEventListener("click", async () => {
  const newMuted = !isMuted;
  muteBtn.disabled = true;

  try {
    const res = await fetch("/mute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ muted: newMuted }),
    });
    const data = await res.json();
    if (data.ok) {
      applyMuteState(data.muted);
    }
  } catch (err) {
    console.error("Mute request failed:", err);
  } finally {
    muteBtn.disabled = false;
  }
});
