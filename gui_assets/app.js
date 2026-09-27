"use strict";

const $ = (id) => document.getElementById(id);
const FIELDS = [
  "server_id", "api_url", "api_key", "model", "stream_edit_interval",
  "category_name", "new_chat_name", "database_path", "system_prompt",
];

// -- status ----------------------------------------------------------------

function setRunning(running) {
  $("dot").className = "dot" + (running ? " on" : "");
  $("statusText").textContent = running ? "Running" : "Stopped";
  $("startBtn").disabled = running;
  $("stopBtn").disabled = !running;
}

let msgTimer = null;
function showMsg(el, ok, text) {
  el.textContent = text || "";
  el.className = "msg " + (ok === true ? "ok" : ok === false ? "bad" : "");
  clearTimeout(msgTimer);
  msgTimer = setTimeout(() => { el.textContent = ""; el.className = "msg"; }, 15000);
}

// -- console ---------------------------------------------------------------

function appendLog(line) {
  const el = $("log");
  const div = document.createElement("div");
  const level = line.includes(" ERROR ") ? "err" : line.includes(" WARNING ") ? "warn" : "";
  div.className = "line" + (level ? " " + level : "");
  div.textContent = line;
  el.appendChild(div);
  while (el.childElementCount > 2000) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

// -- actions ---------------------------------------------------------------

async function loadState() {
  try {
    const state = await pywebview.api.get_state();
    for (const [key, value] of Object.entries(state.config || {})) {
      const el = $("f-" + key);
      if (el) el.value = value ?? "";
    }
    $("f-token").value = state.token || "";
    setRunning(!!state.running);
    appendLog("— manager ready —");
  } catch (err) {
    appendLog("ERROR failed to load state: " + err);
  }
}

async function saveConfig() {
  const values = {};
  for (const f of FIELDS) values[f] = $("f-" + f).value;
  const res = await pywebview.api.save_config(values, $("f-token").value);
  showMsg($("saveMsg"), res.ok, res.message);
}

async function act(fn) {
  try {
    const res = await fn();
    showMsg($("actionMsg"), res.ok, res.message);
  } catch (err) {
    showMsg($("actionMsg"), false, String(err));
  }
}

let resetArmed = false, resetTimer = null;
$("resetBtn").onclick = () => act(async () => {
  if (!resetArmed) {
    resetArmed = true;
    $("resetBtn").textContent = "Really delete everything?";
    resetTimer = setTimeout(disarmReset, 4000);
    return { ok: true, message: "Click again to confirm. This deletes all conversation channels and stored messages." };
  }
  disarmReset();
  return pywebview.api.reset_bot();
});

function disarmReset() {
  resetArmed = false;
  clearTimeout(resetTimer);
  $("resetBtn").textContent = "Reset conversations";
}

$("saveBtn").onclick = () => saveConfig();
$("startBtn").onclick = () => act(() => pywebview.api.start_bot());
$("stopBtn").onclick = () => act(() => pywebview.api.stop_bot());

// -- python push targets -----------------------------------------------------

window.appendLog = appendLog;
window.setRunning = setRunning;
window.onBotExit = (code) => {
  setRunning(false);
  appendLog("— bot process exited (code " + code + ") —");
};

window.addEventListener("pywebviewready", loadState);
