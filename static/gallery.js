const $ = id => document.getElementById(id);
let songs = [], revision = "";
function element(tag, cls, text) {
  const el = document.createElement(tag); el.className = cls || "";
  if(text !== undefined) el.textContent = text; return el;
}
function draw() {
  const query = $("search").value.trim().toLowerCase(), language = $("language").value;
  const filtered = songs.filter(s => (!language || s.language === language) && `${s.title} ${s.genre} ${s.language}`.toLowerCase().includes(query));
  $("gallery").replaceChildren();
  $("release-count").textContent = `${filtered.length} release${filtered.length === 1 ? "" : "s"}`;
  $("gallery-message").hidden = !!filtered.length;
  $("gallery-message").textContent = songs.length ? "No songs match your search." : "Your collection begins with the first automatic release. The next five songs will appear here once published.";
  for(const song of filtered) {
    const card = element("article", "release-card");
    const base = `/media/${encodeURIComponent(song.batchId)}/${song.index}`;
    const art = element("button","release-art"); art.type="button"; art.setAttribute("aria-label",`Play ${song.title}`);
    const img = element("img"); img.src = base + "/cover"; img.alt = song.title + " — cover art"; img.loading = "lazy";
    art.append(img,element("span","play-disc","▶"));
    art.onclick = () => {$("preview-title").textContent=song.title;$("player").src=base+"/video";$("preview").showModal();$("player").play().catch(()=>{});};
    const details = element("div","release-details");
    details.append(element("div","eyebrow",song.language),element("h2","",song.title),element("p","",song.genre));
    const published = new Date(song.published_at).toLocaleString("en-IN",{timeZone:"Asia/Kolkata",dateStyle:"medium",timeStyle:"short"});
    details.append(element("small","",published+" IST"));
    if(/^https:\/\/www\.youtube\.com\/watch\?v=[A-Za-z0-9_-]+$/.test(song.url || "")) {
      const link = element("a","release-link","Listen on YouTube ↗"); link.href=song.url;link.target="_blank";link.rel="noopener";details.append(link);
    }
    card.append(art,details);$("gallery").append(card);
  }
}
async function refresh() {
  try {
    const response=await fetch("/api/releases");if(!response.ok)throw Error();
    const data=await response.json(), signature=JSON.stringify(data);
    if(signature!==revision){revision=signature;songs=data;draw();}
  } catch {$("gallery-message").hidden=false;$("gallery-message").textContent="The studio is offline. Keep Start-Web.bat running, then refresh.";}
}
$("search").addEventListener("input",draw);$("language").addEventListener("change",draw);
$("close-preview").onclick=()=>$("preview").close();
$("preview").addEventListener("close",()=>{$("player").pause();$("player").removeAttribute("src");$("player").load();});
refresh();setInterval(refresh,10000);
