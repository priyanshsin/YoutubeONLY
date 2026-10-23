const token = document.querySelector('meta[name="csrf-token"]').content;
let state = null, signature = "";
const $ = id => document.getElementById(id);
const escape = s => { const el = document.createElement("span"); el.textContent = s ?? ""; return el.innerHTML.replaceAll('"',"&quot;").replaceAll("'","&#39;"); };
const formatTime = value => new Date(typeof value === "number" ? value * 1000 : value).toLocaleString("en-IN", {timeZone:"Asia/Kolkata",dateStyle:"medium",timeStyle:"short"}) + " IST";
function notice(text) { $("notice").hidden = !text; $("notice").textContent = text; }
function countdown() {
  if (!state) return;
  const active = state.batches.find(b => !["complete","failed","interrupted","superseded"].includes(b.status));
  const target = active?.release_at ?? state.next_release;
  const seconds = Math.max(0, Math.floor(target - Date.now()/1000));
  const hours = Math.floor(seconds/3600), minutes = Math.floor(seconds%3600/60), rest = seconds%60;
  $("countdown").textContent = `${String(hours).padStart(2,"0")}:${String(minutes).padStart(2,"0")}:${String(rest).padStart(2,"0")}`;
  $("watch-progress").setAttribute("stroke-dasharray", `${Math.min(100,seconds/864)} 100`);
  $("release-time").textContent = !state.enabled ? "Paused — resume to continue this schedule." :
    seconds ? formatTime(target) : "Release is due — generation and processing may still be finishing.";
}
function draw() {
  $("connection").textContent = state.preview ? "Preview · scheduler off" : state.enabled ? "● Schedule active" : "Schedule paused";
  $("recipient").textContent = state.recipient;
  $("pause").textContent = state.enabled ? "Pause schedule" : "Resume schedule";
  $("pause").disabled = state.preview;
  $("run").disabled = state.preview || state.running || state.batches.some(b=>["failed","interrupted"].includes(b.status));
  $("batch-count").textContent = `${state.batches.length} batch${state.batches.length===1?"":"es"}`;
  if (state.preview) notice("Preview mode: no songs will be generated, published or emailed. Run Start-Web.bat to activate the 24-hour schedule.");
  const nextSignature = JSON.stringify(state.batches);
  if (state.batches.length && signature !== nextSignature) {
    signature = nextSignature;
    $("batches").innerHTML = state.batches.map(batch => `
      <section class="batch"><div class="batch-head"><div><h3>Release ${escape(batch.id.slice(0,8))}</h3><small>Scheduled ${escape(formatTime(batch.release_at))}</small></div><span class="badge">${escape(batch.status)}</span></div>
      <div class="songs">${batch.songs.map(song => {
        const playable = ["ready","publishing","published"].includes(song.status);
        const base = `/media/${encodeURIComponent(batch.id)}/${song.index}`;
        return `<article class="song"><div class="art">${playable?`<img src="${base}/cover" alt="Song artwork">`:"♪"}</div>
        <h4>${escape(song.title)}</h4><p>${escape(song.language)}</p><p>${escape(song.genre)} · ${escape(song.status)}</p>
        ${song.published_at?`<p>${escape(formatTime(song.published_at))}</p>`:""}
        ${playable?`<button class="secondary preview-song" data-video="${base}/video" data-title="${escape(song.title)}">▶ Preview</button>`:""}
        ${song.url?.startsWith("https://www.youtube.com/watch?v=")?`<a href="${escape(song.url)}" target="_blank" rel="noopener">YouTube ↗</a>`:""}</article>`;
      }).join("")}</div><div class="batch-foot">Email: ${batch.email.status==="sent"?"Accepted by Resend with MP3 attachments":escape(batch.email.status)}
      ${batch.error?`<p class="error">${escape(batch.error)}</p><button class="secondary retry" data-id="${escape(batch.id)}">Retry unfinished work</button>`:""}</div></section>`).join("");
  }
  $("logs").textContent = state.logs.map(l=>`${formatTime(l.at)}  ${l.message}`).join("\n") || "Waiting for activity…";
  countdown();
}
async function refresh() {
  try { const r = await fetch("/api/status"); if(!r.ok) throw Error("Cannot read status."); state = await r.json(); draw(); }
  catch { $("connection").textContent = "Disconnected"; notice("The local server is unavailable. Keep Start-Web.bat running and refresh this page."); }
}
async function action(name, id) {
  try {
    const r = await fetch("/api/action", {method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":token},body:JSON.stringify({action:name,id})});
    const result = await r.json(); if(!r.ok) throw Error(result.error || "Action failed.");
    notice(""); await refresh();
  } catch(e) { notice(e.message); }
}
$("pause").onclick = () => action(state.enabled?"pause":"resume");
$("run").onclick = () => { if(confirm("Generate and publicly publish two songs now, then email both MP3s? API charges apply.")) action("run"); };
$("batches").addEventListener("click", e => {
  const button = e.target.closest("button");
  if(!button) return;
  if(button.classList.contains("preview-song")) {
    $("preview-title").textContent = button.dataset.title;
    $("player").src = button.dataset.video;
    $("preview").showModal();
    $("player").play().catch(()=>{});
  }
  if(button.classList.contains("retry") && confirm("Resume unfinished work? Existing videos will not be reuploaded.")) action("retry",button.dataset.id);
});
$("close-preview").onclick = () => $("preview").close();
$("preview").addEventListener("close",()=>{$("player").pause();$("player").removeAttribute("src");$("player").load();});
setInterval(countdown,1000);setInterval(refresh,5000);refresh();
