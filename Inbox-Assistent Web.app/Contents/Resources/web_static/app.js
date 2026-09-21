(() => {
  "use strict";
  const token = new URLSearchParams(location.search).get("token") || "";
  const $ = (selector) => document.querySelector(selector);
  const selected = new Set();
  let state = {folder: null, images: [], groups: [], completed: []};
  let previewId = null;
  let busy = false;
  let noticeTimer = null;
  let noticeShown = false;
  const MAX_IMPORT_SIZE = 120 * 1024 * 1024;

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[ch]);
  const imageById = (id) => state.images.find(image => image.id === id);
  // Die Version ändert sich nur, wenn sich das Bild ändert (Datei, Drehung, Korrektur).
  // Vorher hing Date.now() an: jedes Neuzeichnen lud alle Vorschauen neu.
  const version = (id) => encodeURIComponent(imageById(id)?.version || "");
  const imageUrl = (id) => `/api/thumb/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}&v=${version(id)}`;
  const previewUrl = (id) => `/api/image/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}&v=${version(id)}`;

  async function api(path, data = {}) {
    const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json","X-App-Token":token}, body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Aktion fehlgeschlagen.");
    return result;
  }

  async function refresh() {
    const response = await fetch("/api/state", {headers:{"X-App-Token":token}});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Entwurf konnte nicht geladen werden.");
    state = result;
    render();
    if (state.notice && !noticeShown) { noticeShown = true; notify(state.notice, "error", true); }
  }

  function notify(message, kind = "info", sticky = false) {
    const box = $("#notice");
    box.textContent = message;
    box.className = `notice ${kind}`;
    box.hidden = false;
    clearTimeout(noticeTimer);
    if (!sticky) noticeTimer = setTimeout(() => box.hidden = true, 6500);
  }

  async function run(action, message = "Bitte warten …") {
    if (busy) { notify("Es läuft noch ein Vorgang. Bitte kurz warten.", "info"); return null; }
    busy = true;
    document.body.classList.add("busy");
    notify(message, "info", true);
    try {
      const result = await action();
      if (result && result.state) state = result.state;
      else if (result && result.folder !== undefined) state = result;
      render();
      return result;
    } catch (error) {
      notify(error.message || String(error), "error", true);
      return null;
    } finally {
      busy = false;
      document.body.classList.remove("busy");
      render();
    }
  }

  const pause = (ms) => new Promise(resolve => setTimeout(resolve, ms));

  // Startet einen Hintergrundvorgang und fragt seinen Fortschritt ab, bis er fertig ist.
  // Die Oberfläche bleibt währenddessen erreichbar; Änderungen sperrt der Server.
  async function runJob(path, data, message) {
    if (busy) { notify("Es läuft noch ein Vorgang. Bitte kurz warten.", "info"); return null; }
    busy = true;
    document.body.classList.add("busy");
    render();
    notify(message, "info", true);
    try {
      const {job} = await api(path, data);
      for (;;) {
        await pause(500);
        const response = await fetch(`/api/job?id=${encodeURIComponent(job)}`, {headers:{"X-App-Token":token}});
        const status = await response.json();
        if (!response.ok) throw new Error(status.error || "Vorgang nicht gefunden.");
        if (status.status === "error") throw new Error(status.error || "Vorgang fehlgeschlagen.");
        if (status.status === "done") return status.result;
        const {done, total, label} = status.progress;
        if (total) notify(`${label || message} (${done}/${total})`, "info", true);
      }
    } catch (error) {
      notify(error.message || String(error), "error", true);
      return null;
    } finally {
      busy = false;
      document.body.classList.remove("busy");
      await refresh().catch(() => render());
    }
  }

  function photoCard(image, index = null, isOpen = false) {
    const missing = image.missing;
    const checked = selected.has(image.id);
    return `<article class="photo-card ${checked ? "selected" : ""} ${missing ? "missing" : ""}" draggable="${!missing}" data-photo="${esc(image.id)}" ${index === null ? "" : `data-index="${index}"`}>
      ${isOpen ? `<input class="photo-check" type="checkbox" aria-label="${esc(image.name)} auswählen" ${checked ? "checked" : ""}>` : ""}
      ${missing ? `<div class="empty">Foto fehlt</div>` : `<img class="photo-image" src="${imageUrl(image.id)}" alt="Vorschau ${esc(image.name)}" loading="lazy">`}
      ${index === null ? "" : `<div class="page-number">SEITE ${String(index + 1).padStart(2,"0")}</div>`}
      <div class="filename" title="${esc(image.name)}">${esc(image.name)}</div>
      <div class="date">${esc(image.fallback_date)} · ${esc(image.date_origin)}</div>
      ${index === null ? "" : `<div class="page-controls"><button class="text-button move-left" type="button" aria-label="Seite nach links">←</button><button class="text-button move-right" type="button" aria-label="Seite nach rechts">→</button></div>`}
    </article>`;
  }

  // Das aktive Eingabefeld einer Gruppe merken. render() ersetzt das HTML komplett;
  // ohne das springt der Fokus nach Tab auf <body> und die nächste Eingabe geht verloren.
  function captureFocus() {
    const active = document.activeElement;
    if (!active?.matches?.(".group-fields input")) return null;
    return {group: active.closest(".group-card").dataset.group, name: active.name, value: active.value,
            start: active.selectionStart, end: active.selectionEnd};
  }

  function restoreFocus(saved) {
    if (!saved) return;
    const input = document.querySelector(`.group-card[data-group="${CSS.escape(saved.group)}"] .group-fields input[name="${saved.name}"]`);
    if (!input) return;
    input.value = saved.value;  // noch nicht gespeicherte Eingabe behalten
    input.focus();
    try { input.setSelectionRange(saved.start, saved.end); } catch { /* type=date kennt keine Auswahl */ }
  }

  function render() {
    const focus = captureFocus();
    const grouped = new Set(state.groups.flatMap(group => group.pages));
    const open = state.images.filter(image => !grouped.has(image.id));
    for (const id of [...selected]) if (!open.some(image => image.id === id)) selected.delete(id);
    $("#folder-label").textContent = state.folder || "Bitte zuerst einen Fotoordner auswählen";
    $("#folder-label").title = state.folder || "";
    $("#draft-badge").textContent = state.folder ? "Lokal gespeichert" : "Kein Entwurf";
    $("#draft-badge").classList.toggle("muted", !state.folder);
    $("#photo-count").textContent = state.images.length;
    $("#open-count").textContent = open.length;
    $("#ready-count").textContent = state.groups.filter(group => group.ready).length;
    $("#done-count").textContent = state.completed.length;
    $("#manual-group").disabled = !selected.size || busy;
    $("#ai-group").disabled = !selected.size || busy;
    $("#export").disabled = busy;
    $("#browse-files").disabled = !state.folder || busy;
    $("#drop-zone").classList.toggle("disabled", !state.folder);
    $("#ungrouped").innerHTML = open.length ? open.map(image => photoCard(image, null, true)).join("") : `<div class="empty">${state.images.length ? "Alle importierten Seiten sind einer Gruppe zugeordnet." : "Noch keine Fotoseiten importiert."}</div>`;

    $("#groups").innerHTML = state.groups.length ? state.groups.map((group, position) => {
      const filename = group.filename || "Dateiname nach vollständiger Eingabe verfügbar";
      const pages = group.pages.map((id, index) => imageById(id) ? photoCard(imageById(id), index) : "").join("");
      const review = group.source === "ai" && !group.confirmed;
      const reason = group.blocked_reason || (group.needs_review ? group.reason : "");
      const autoWarnings = group.pages.filter(id => imageById(id)?.auto_review).length;
      return `<article class="group-card" data-group="${esc(group.id)}">
        <div class="group-top"><div><span class="group-index">BRIEF ${String(position + 1).padStart(2,"0")} · ${group.pages.length} ${group.pages.length === 1 ? "SEITE" : "SEITEN"}</span><h3>${group.source === "ai" ? "ChatGPT-Vorschlag" : "Manuelle Gruppe"}</h3></div><div class="group-actions"><span class="status ${group.ready ? "" : "warn"}">${group.ready ? "Bereit zum Export" : "Prüfen"}</span><button class="text-button danger ungroup-button" type="button">Auflösen</button></div></div>
        <div class="group-fields"><div class="field"><label>DATUM</label><input name="date" type="date" value="${esc(group.date)}"></div><div class="field"><label>ORGANISATION</label><input name="sender" value="${esc(group.sender)}" placeholder="Absenderorganisation" maxlength="70"></div><div class="field"><label>TITEL / BETREFF</label><input name="title" value="${esc(group.title)}" placeholder="Betreff des Briefs" maxlength="100"></div></div>
        <div class="filename-preview">PDF: ${esc(filename)}</div>
        ${reason ? `<div class="group-warning">${esc(reason)}</div>` : ""}
        ${autoWarnings ? `<div class="group-warning">Bei ${autoWarnings} ${autoWarnings === 1 ? "Seite" : "Seiten"} war eine automatische Begradigung nicht eindeutig. Bitte Vorschau prüfen.</div>` : ""}
        <div class="page-strip drop-target" data-target="${esc(group.id)}">${pages || `<div class="empty">Seiten hierher ziehen</div>`}</div>
        <div class="group-bottom"><small>${group.source === "ai" ? `Hinweise: ${esc(group.evidence || "Vorschlag prüfen")}` : "Seiten per Drag-and-drop oder Pfeiltasten sortieren."}</small>${review ? `<button class="button primary confirm-group" type="button">Vorschlag bestätigen</button>` : ""}</div>
      </article>`;
    }).join("") : `<div class="empty">Noch keine Gruppen erstellt. Wähle oben zusammengehörige Fotoseiten aus.</div>`;

    restoreFocus(focus);

    $("#completed").hidden = !state.completed.length;
    $("#completed-list").innerHTML = state.completed.map(item => `<div class="completed-item"><span>✓ ${esc(item.pdf)}</span><button class="text-button reveal" data-id="${esc(item.id)}" type="button">Im Finder zeigen</button></div>`).join("");
  }

  async function importFiles(files) {
    if (!state.folder) return notify("Bitte zuerst den Quellordner auswählen.", "error");
    if (!files.length) return;
    if (busy) return notify("Es läuft noch ein Vorgang. Bitte kurz warten.", "info");
    let imported = 0;
    const errors = [];
    busy = true;
    notify(`${files.length} ${files.length === 1 ? "Foto wird" : "Fotos werden"} geprüft …`, "info", true);
    for (const file of files) {
      // Vorab prüfen: einen riesigen Upload erst zu senden, um ihn dann abzulehnen, kostet nur Zeit.
      if (file.size > MAX_IMPORT_SIZE) { errors.push(`${file.name}: Das Foto ist zu groß (maximal 120 MB).`); continue; }
      try {
        const response = await fetch("/api/import", {method:"POST",headers:{"X-App-Token":token,"X-Filename":encodeURIComponent(file.name),"Content-Type":"application/octet-stream"},body:file});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Import fehlgeschlagen.");
        state = result;
        imported++;
      } catch (error) { errors.push(`${file.name}: ${error.message}`); }
    }
    busy = false;
    render();
    if (errors.length) notify(`${imported} importiert. ${errors.join(" | ")}`, "error", true);
    else notify(`${imported} ${imported === 1 ? "Foto" : "Fotos"} im Entwurf.`, "success");
  }

  function openPreview(id) {
    const image = imageById(id);
    if (!image || image.missing) return;
    previewId = id;
    $("#preview-title").textContent = image.name;
    $("#preview-image").src = previewUrl(id);
    $(".preview-image-wrap").classList.remove("zoomed");
    $("#zoom-preview").textContent = "Vergrößern";
    $("#reset-correction").disabled = !image.auto_rotation && !image.auto_quad && !image.auto_angle;
    $("#preview-dialog").showModal();
  }

  async function rotate(degrees) {
    if (!previewId) return;
    const result = await run(() => api("/api/rotate", {id:previewId, degrees}), "Vorschau wird gedreht …");
    if (result) {
      $("#preview-image").src = previewUrl(previewId);
      notify("Drehung für die PDF gespeichert.", "success");
    }
  }

  function renderResults(result) {
    const sections = [];
    if (result.exported.length) sections.push(`<div class="result-section"><h3>${result.exported.length} ${result.exported.length === 1 ? "PDF exportiert" : "PDFs exportiert"}</h3>${result.exported.map(x => `<div class="result-item"><span>✓ ${esc(x.pdf)}</span>${x.warnings.length ? `<small>${esc(x.warnings.join(" "))}</small>` : ""}</div>`).join("")}</div>`);
    if (result.skipped.length) sections.push(`<div class="result-section"><h3>${result.skipped.length} ${result.skipped.length === 1 ? "Gruppe benötigt" : "Gruppen benötigen"} Prüfung</h3>${result.skipped.map(x => `<div class="result-item"><span>${esc(x.group)}</span><small>${esc(x.reason)}</small></div>`).join("")}</div>`);
    if (result.ungrouped.length) sections.push(`<div class="result-section"><h3>${result.ungrouped.length} ungruppierte ${result.ungrouped.length === 1 ? "Seite" : "Seiten"}</h3>${result.ungrouped.map(name => `<div class="result-item"><span>${esc(name)}</span><small>Nicht bearbeitet</small></div>`).join("")}</div>`);
    if (!sections.length) sections.push(`<div class="empty">Noch keine Fotos zum Exportieren vorhanden.</div>`);
    $("#result-content").innerHTML = sections.join("");
    $("#results").hidden = false;
    $("#results").scrollIntoView({behavior:"smooth", block:"start"});
  }

  $("#choose-folder").addEventListener("click", async () => {
    const result = await run(() => api("/api/select-folder"), "Ordnerauswahl wird geöffnet …");
    if (result?.cancelled) notify("Ordnerauswahl abgebrochen.");
    else if (result) { selected.clear(); $("#results").hidden = true; notify("Fotoordner ausgewählt. Du kannst jetzt Fotos hineinziehen.", "success"); }
  });
  $("#quit").addEventListener("click", async () => {
    const result = await run(() => api("/api/quit"), "Entwurf wird geschlossen …");
    if (result) {
      document.querySelector(".main").innerHTML = `<div class="folder-panel"><div class="folder-copy"><span class="eyebrow">LOKAL GESPEICHERT</span><h1>App beendet</h1><p>Du kannst diesen Browsertab schließen und den Starter später erneut öffnen.</p></div></div>`;
    }
  });
  $("#browse-files").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", event => { importFiles([...event.target.files]); event.target.value = ""; });
  // Dateien, die neben der Ablagefläche losgelassen werden, öffnete der Browser
  // bisher selbst – der App-Tab war dann weg. Überall sonst den Standard unterbinden.
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, event => {
      if (event.dataTransfer?.types?.includes("Files") && !event.target.closest?.("#drop-zone")) event.preventDefault();
    });
  }
  const zone = $("#drop-zone");
  zone.addEventListener("dragover", event => {event.preventDefault(); zone.classList.add("drag-over");});
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", event => {event.preventDefault(); zone.classList.remove("drag-over"); importFiles([...event.dataTransfer.files]);});

  $("#manual-group").addEventListener("click", async () => {
    const result = await run(() => api("/api/group", {ids:[...selected]}), "Gruppe wird erstellt …");
    if (result) {selected.clear(); render(); notify("Gruppe erstellt. Bitte Datum, Organisation und Titel prüfen.", "success");}
  });
  $("#ai-group").addEventListener("click", async () => {
    if (!confirm(`Diese ${selected.size} ausgewählten Fotos, ihre Dateinamen und lokal erkannter Text werden zur Analyse an OpenAI übertragen. Nur fortfahren, wenn sie nicht sensibel sind. Fortfahren?`)) return;
    const result = await runJob("/api/ai", {ids:[...selected], consent:true}, "ChatGPT analysiert die ausgewählten Fotos. Das kann etwas dauern …");
    if (result) {selected.clear(); render(); notify("ChatGPT-Vorschläge sind da. Bitte jede Gruppe prüfen und bestätigen.", "success", true);}
  });
  $("#export").addEventListener("click", async () => {
    if (!state.folder) return notify("Bitte zuerst einen Quellordner auswählen.", "error");
    const result = await runJob("/api/export", {}, "PDFs werden lokal erstellt und per Texterkennung durchsuchbar gemacht …");
    if (result) {renderResults(result); notify(result.exported.length ? `${result.exported.length} ${result.exported.length === 1 ? "PDF wurde" : "PDFs wurden"} exportiert.` : "Keine Gruppe war exportbereit. Siehe Ergebnisübersicht.", result.exported.length ? "success" : "info", true);}
  });

  document.addEventListener("change", async event => {
    if (event.target.matches(".photo-check")) {
      const id = event.target.closest(".photo-card").dataset.photo;
      if (event.target.checked) selected.add(id); else selected.delete(id);
      render();
    }
    if (event.target.matches(".group-fields input")) {
      const group = event.target.closest(".group-card");
      const fields = Object.fromEntries([...group.querySelectorAll(".group-fields input")].map(input => [input.name, input.value]));
      const result = await run(() => api("/api/update-group", {id:group.dataset.group, ...fields}), "Änderung wird gespeichert …");
      if (result) notify("Entwurf gespeichert.", "success");
    }
  });
  document.addEventListener("click", async event => {
    const photo = event.target.closest(".photo-card");
    if (event.target.matches(".photo-image") && photo) return openPreview(photo.dataset.photo);
    const group = event.target.closest(".group-card");
    if (event.target.matches(".ungroup-button") && group) {
      const result = await run(() => api("/api/ungroup", {id:group.dataset.group}), "Gruppe wird aufgelöst …");
      if (result) notify("Seiten sind wieder ungruppiert.", "success");
    }
    if (event.target.matches(".confirm-group") && group) {
      const fields = Object.fromEntries([...group.querySelectorAll(".group-fields input")].map(input => [input.name, input.value]));
      const result = await run(() => api("/api/update-group", {id:group.dataset.group, ...fields, confirm:true}), "Vorschlag wird bestätigt …");
      if (result) notify("KI-Vorschlag bestätigt.", "success");
    }
    if ((event.target.matches(".move-left") || event.target.matches(".move-right")) && group && photo) {
      const current = state.groups.find(item => item.id === group.dataset.group);
      const oldIndex = current.pages.indexOf(photo.dataset.photo);
      const newIndex = oldIndex + (event.target.matches(".move-left") ? -1 : 1);
      if (newIndex < 0 || newIndex >= current.pages.length) return;
      // Der Server entfernt die Seite vor dem Einfügen; newIndex passt damit in beide Richtungen.
      await run(() => api("/api/move", {image_id:photo.dataset.photo, target_id:current.id, index:newIndex}), "Reihenfolge wird gespeichert …");
    }
    if (event.target.matches(".reveal")) {
      await run(() => api("/api/reveal", {id:event.target.dataset.id}), "PDF wird im Finder angezeigt …");
    }
  });

  // Vorschau nicht renderbar (Datei unlesbar, gerade umbenannt …): Hinweis statt Bruch-Symbol.
  // Fehler-Events steigen nicht auf, daher in der Capture-Phase abfangen.
  document.addEventListener("error", event => {
    if (!event.target.matches?.(".photo-image")) return;
    const hint = document.createElement("div");
    hint.className = "empty";
    hint.textContent = "Vorschau nicht verfügbar";
    event.target.replaceWith(hint);
  }, true);

  document.addEventListener("dragstart", event => {
    const card = event.target.closest(".photo-card");
    if (!card) return;
    event.dataTransfer.setData("text/plain", card.dataset.photo);
    event.dataTransfer.effectAllowed = "move";
  });
  for (const container of [$("#ungrouped"), $("#groups")]) {
    container.addEventListener("dragover", event => {
      if (!event.target.closest(".drop-target")) return;
      event.preventDefault();
      event.target.closest(".drop-target").classList.add("drop-over");
    });
    container.addEventListener("dragleave", event => event.target.closest(".drop-target")?.classList.remove("drop-over"));
    container.addEventListener("drop", async event => {
      const target = event.target.closest(".drop-target");
      if (!target) return;
      event.preventDefault();
      target.classList.remove("drop-over");
      const id = event.dataTransfer.getData("text/plain");
      if (!imageById(id)) return;
      const targetId = target.dataset.target || null;
      const card = event.target.closest(".photo-card");
      let index = card?.dataset.index === undefined ? null : Number(card.dataset.index);
      if (targetId && index !== null) {
        const source = state.groups.find(group => group.pages.includes(id));
        if (source?.id === targetId && source.pages.indexOf(id) < index) index--;
      }
      await run(() => api("/api/move", {image_id:id,target_id:targetId,index}), "Seitenzuordnung wird gespeichert …");
    });
  }

  $("#close-preview").addEventListener("click", () => $("#preview-dialog").close());
  $("#preview-dialog").addEventListener("close", () => previewId = null);
  $("#rotate-left").addEventListener("click", () => rotate(90));
  $("#rotate-right").addEventListener("click", () => rotate(-90));
  $("#rotate-half").addEventListener("click", () => rotate(180));
  $("#zoom-preview").addEventListener("click", () => {
    const zoomed = $(".preview-image-wrap").classList.toggle("zoomed");
    $("#zoom-preview").textContent = zoomed ? "Einpassen" : "Vergrößern";
  });
  $("#reset-correction").addEventListener("click", async () => {
    if (!previewId) return;
    const result = await run(() => api("/api/reset-correction", {id:previewId}), "Auto-Korrektur wird zurückgesetzt …");
    if (result) {$("#preview-image").src = previewUrl(previewId); $("#reset-correction").disabled = true; notify("Automatische Korrektur zurückgesetzt.", "success");}
  });
  refresh().catch(error => notify(error.message, "error", true));
})();
