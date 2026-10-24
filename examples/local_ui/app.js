"use strict";
const $ = (id) => document.getElementById(id);
let token = location.hash.slice(1) || sessionStorage.getItem("code-attach-token") || "";
if (location.hash) { sessionStorage.setItem("code-attach-token", token); history.replaceState(null, "", location.pathname); }
let taskId = null, cursor = 0, terminal = true, polling = false;
const decisions = new Set();
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? "GET" : "POST", headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  if (!response.ok) { let detail; try { detail = (await response.json()).detail; } catch {} throw new Error(`${response.status}: ${typeof detail === "string" ? detail : response.statusText}`); }
  return response.json();
}
function state(value) { $("state").textContent = value; $("start").disabled = !terminal; $("cancel").disabled = terminal; $("instruction").disabled = !terminal; }
function eventCard(title, content, approval = false) {
  const card = document.createElement("article"); card.className = "event" + (approval ? " approval" : "");
  const heading = document.createElement("h3"); heading.textContent = title;
  const pre = document.createElement("pre"); pre.textContent = content;
  card.append(heading, pre); $("events").append(card); return card;
}
function render(event) {
  const d = event.data;
  if (event.type === "assistant") eventCard("ASSISTANT", d.text);
  else if (event.type === "tool_result") {
    for (const row of decisions) if (row.dataset.tool === d.tool) { row.replaceChildren(document.createTextNode("処理済み")); decisions.delete(row); }
    eventCard(d.tool, JSON.stringify(d.result || {status: d.status}, null, 2));
  }
  else if (event.type === "state") eventCard(d.state, d.detail || "作業が終了しました。テスト結果は実行結果の欄で確認できます。");
  else if (event.type === "approval_required") {
    const metadata = d.summary.kind === "git_commit" ? JSON.stringify({...d.summary, diff: undefined}, null, 2) + "\n\n" : (d.summary.encoding ? "文字コード: " + d.summary.encoding + "\n\n" : "");
    const content = d.summary.diff ? metadata + d.summary.diff : JSON.stringify(d.summary, null, 2);
    const card = eventCard("承認待ち / " + d.tool, content, true);
    const row = document.createElement("div"); row.className = "actions"; row.dataset.tool = d.tool;
    for (const [label, approve] of [["承認して実行", true], ["却下", false]]) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = label; if (!approve) button.className = "secondary";
      button.addEventListener("click", async () => {
        row.querySelectorAll("button").forEach(b => b.disabled = true); $("error").textContent = "";
        try { await api(`/api/code/tasks/${taskId}/decision`, {action_id: d.action_id, fingerprint: d.fingerprint, approve}); decisions.delete(row); row.replaceChildren(document.createTextNode(approve ? "承認済み" : "却下済み")); }
        catch (error) { $("error").textContent = error.message; if (!terminal) row.querySelectorAll("button").forEach(b => b.disabled = false); }
      }); row.append(button);
    }
    decisions.add(row); card.append(row);
  }
}
async function poll() {
  if (polling || !taskId || terminal) return;
  polling = true;
  try {
    const data = await api(`/api/code/tasks/${taskId}/events?after=${cursor}`);
    for (const event of data.events) { render(event); cursor = Number(event.id); }
    terminal = data.terminal; state(data.state);
    if (terminal) { for (const row of decisions) row.querySelectorAll("button").forEach(b => b.disabled = true); decisions.clear(); sessionStorage.removeItem("code-attach-task"); }
    $("error").textContent = "";
  } catch (error) { $("error").textContent = "接続できません: " + error.message; if (error.message.startsWith("404:")) { terminal = true; state("作業が見つかりません。再起動後は新しい作業を開始してください。"); sessionStorage.removeItem("code-attach-task"); } }
  finally { polling = false; if (!terminal) setTimeout(poll, 600); }
}
$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("error").textContent = ""; terminal = false; state("開始しています…");
  try {
    const data = await api("/api/code/tasks", {workspace_id: "project", instruction: $("instruction").value, idempotency_key: crypto.randomUUID()});
    taskId = data.task_id; cursor = 0; decisions.clear(); $("events").replaceChildren(); sessionStorage.setItem("code-attach-task", taskId); poll();
  } catch (error) { terminal = true; state("開始できませんでした"); $("error").textContent = error.message; }
});
$("cancel").addEventListener("click", async () => { $("cancel").disabled = true; try { await api(`/api/code/tasks/${taskId}/cancel`, {}); } catch (error) { $("error").textContent = error.message; $("cancel").disabled = false; } });
(async () => {
  try {
    const config = await api("/api/config"); $("workspace").textContent = "ワークスペース: " + config.workspace;
    $("log-dir").textContent = "ログ保存先: " + config.log_dir;
    $("mode").textContent = config.demo ? "デモモード · モデル呼び出しなし / 実ファイル編集とテスト実行" : "モデル接続モード";
    if (config.demo) $("instruction").value = "calc.py の add の計算を直し、テストを実行して";
    taskId = sessionStorage.getItem("code-attach-task"); terminal = !taskId; state(taskId ? "作業を再接続しています…" : "準備完了"); if (taskId) { $("events").replaceChildren(); poll(); }
  } catch (error) { $("error").textContent = "起動時に表示されたURLから開いてください。 " + error.message; state("未接続"); $("start").disabled = true; }
})();
