const state = {
    jobs: [],
    inspection: null,
    selectedJobId: null,
    polling: null,
    clock: null,
    submitting: false,
};

const activeStatuses = new Set(["queued", "downloading", "preparing", "transcribing", "exporting"]);

const statusLabels = {
    queued: "En attente",
    downloading: "Téléchargement",
    preparing: "Préparation",
    transcribing: "Transcription",
    exporting: "Création des fichiers",
    completed: "Terminé",
    failed: "Erreur",
};

async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "Une erreur est survenue.");
    return data;
}

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function formatDuration(totalSeconds) {
    const safeSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    const hours = Math.floor(safeSeconds / 3600);
    const minutes = Math.floor((safeSeconds % 3600) / 60);
    const seconds = safeSeconds % 60;
    return [hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");
}

function formatReadableDuration(totalSeconds) {
    if (!Number.isFinite(Number(totalSeconds))) return "Durée inconnue";
    const seconds = Math.max(0, Math.round(Number(totalSeconds)));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    if (hours) return `${hours} h ${String(minutes).padStart(2, "0")} min`;
    return `${minutes} min ${String(remainder).padStart(2, "0")} s`;
}

function secondsSince(value, now = Date.now()) {
    const timestamp = Date.parse(value || "");
    return Number.isFinite(timestamp) ? Math.max(0, (now - timestamp) / 1000) : 0;
}

function estimateRemaining(job, now = Date.now()) {
    if (job.status === "completed") return "00:00:00";
    if (job.status !== "transcribing") return null;
    const completed = Number(job.chunks_completed || 0);
    const total = Number(job.chunk_count || 0);
    const processingTotal = Number(job.chunk_processing_seconds_total || 0);
    const concurrency = Math.max(1, Number(job.api_concurrency || 1));
    if (completed < 1 || total < 1 || processingTotal <= 0) return null;

    const average = processingTotal / completed;
    const remainingChunks = Math.max(0, total - completed);
    const waves = Math.ceil(remainingChunks / concurrency);
    const sinceLastCompletion = secondsSince(job.last_chunk_completed_at, now);
    return formatDuration(Math.max(0, average * waves - Math.min(average, sinceLastCompletion)));
}

function updateLiveCounters() {
    const now = Date.now();
    document.querySelectorAll("[data-job-timer]").forEach((timer) => {
        const status = timer.dataset.status;
        const createdAt = timer.dataset.createdAt;
        const updatedAt = timer.dataset.updatedAt;
        const completedAt = timer.dataset.completedAt;
        if (activeStatuses.has(status)) {
            timer.textContent = `Total ${formatDuration(secondsSince(createdAt, now))} · Étape ${formatDuration(secondsSince(updatedAt, now))}`;
        } else {
            const end = Date.parse(completedAt || updatedAt || "");
            const start = Date.parse(createdAt || "");
            timer.textContent = `Durée ${Number.isFinite(end) && Number.isFinite(start) ? formatDuration((end - start) / 1000) : "--:--:--"}`;
        }
    });

    document.querySelectorAll("[data-job-eta]").forEach((element) => {
        const job = state.jobs.find((item) => item.id === element.dataset.jobEta);
        if (!job) return;
        const remaining = estimateRemaining(job, now);
        if (remaining) element.textContent = `Restant estimé ${remaining}`;
        else if (job.status === "transcribing") element.textContent = "Estimation après la première partie";
        else if (activeStatuses.has(job.status)) element.textContent = "Estimation en préparation";
        else element.textContent = "";
    });
}

function updateSubmitState() {
    const button = document.querySelector("#submitJob");
    const hasActiveJob = state.jobs.some((job) => activeStatuses.has(job.status));
    button.disabled = state.submitting || hasActiveJob;
    if (state.submitting) button.textContent = state.inspection ? "Démarrage…" : "Analyse…";
    else if (hasActiveJob) button.textContent = "Traitement déjà en cours";
    else button.textContent = state.inspection ? "Confirmer et démarrer" : "Analyser la vidéo";
}

// ---- Plage à transcrire + aperçu vidéo ----
function parseClock(text) {
    const value = String(text || "").trim();
    if (!value) return null;
    if (!/^\d+(:\d{1,2}){0,2}$/.test(value)) return NaN;
    const parts = value.split(":").map(Number);
    if (parts.length > 1 && parts.slice(1).some((part) => part > 59)) return NaN;
    return parts.reduce((total, part) => total * 60 + part, 0);
}

const preview = { player: null, apiPromise: null, videoId: null };

function currentRange() {
    const duration = state.inspection ? state.inspection.duration : null;
    const start = parseClock(document.querySelector("#rangeStart").value);
    const end = parseClock(document.querySelector("#rangeEnd").value);
    return { start, end, duration };
}

function rangeProblem({ start, end, duration }) {
    if (Number.isNaN(start) || Number.isNaN(end)) return "Format attendu : hh:mm:ss (ou mm:ss).";
    if (duration && start !== null && start >= duration) return "Le début doit être avant la fin de la vidéo.";
    if (duration === null && end !== null) return "Durée inconnue : indiquez seulement un début.";
    if (start !== null && end !== null && end - start < 5) return "La plage doit durer au moins 5 secondes.";
    return null;
}

function effectiveSeconds({ start, end, duration }) {
    if (!duration) return null;
    const stop = end !== null && end < duration - 1 ? end : duration;
    return Math.max(0, stop - (start || 0));
}

function updateRangeSummary() {
    const summary = document.querySelector("#rangeSummary");
    const range = currentRange();
    const problem = rangeProblem(range);
    summary.className = problem ? "range-summary error" : "range-summary";
    if (problem) {
        summary.textContent = problem;
    } else {
        const seconds = effectiveSeconds(range);
        if (seconds === null || !range.duration || seconds >= range.duration - 1) {
            summary.textContent = range.duration ? "Toute la vidéo sera transcrite." : "";
        } else {
            const saved = Math.round((1 - seconds / range.duration) * 100);
            summary.textContent = `Durée transcrite : ${formatReadableDuration(seconds)} (${saved} % d’audio en moins).`;
        }
    }
    const confirmBox = document.querySelector("#longConfirmation");
    if (state.inspection && state.inspection.duration) {
        const seconds = effectiveSeconds(range);
        const needsConfirmation = seconds === null || seconds >= 2 * 60 * 60;
        confirmBox.classList.toggle("hidden", !needsConfirmation);
        state.needsConfirmation = needsConfirmation;
    }
}

function syncSlidersFromFields() {
    const duration = state.inspection && state.inspection.duration ? Math.floor(state.inspection.duration) : 0;
    const start = parseClock(document.querySelector("#rangeStart").value);
    const end = parseClock(document.querySelector("#rangeEnd").value);
    const sliderStart = document.querySelector("#sliderStart");
    const sliderEnd = document.querySelector("#sliderEnd");
    sliderStart.value = Number.isFinite(start) ? Math.min(start, duration) : 0;
    sliderEnd.value = Number.isFinite(end) ? Math.min(end, duration) : duration;
    document.querySelector("#sliderStartOut").textContent = formatDuration(sliderStart.value);
    document.querySelector("#sliderEndOut").textContent = formatDuration(sliderEnd.value);
}

function setRangeField(which, seconds) {
    const duration = state.inspection && state.inspection.duration ? Math.floor(state.inspection.duration) : null;
    const field = document.querySelector(which === "start" ? "#rangeStart" : "#rangeEnd");
    const rounded = Math.max(0, Math.floor(seconds));
    if (which === "start" && rounded === 0) field.value = "";
    else if (which === "end" && duration !== null && rounded >= duration) field.value = "";
    else field.value = formatDuration(rounded);
    syncSlidersFromFields();
    updateRangeSummary();
}

function loadYouTubeApi() {
    if (window.YT && window.YT.Player) return Promise.resolve();
    if (preview.apiPromise) return preview.apiPromise;
    preview.apiPromise = new Promise((resolve, reject) => {
        window.onYouTubeIframeAPIReady = () => resolve();
        const script = document.createElement("script");
        script.src = "https://www.youtube.com/iframe_api";
        script.onerror = () => {
            preview.apiPromise = null;
            reject(new Error("Impossible de charger le lecteur YouTube (connexion ou blocage réseau)."));
        };
        document.head.appendChild(script);
    });
    return preview.apiPromise;
}

async function openPreview() {
    const note = document.querySelector("#previewNote");
    const panel = document.querySelector("#previewPanel");
    if (!state.inspection) return;
    panel.classList.remove("hidden");
    const duration = state.inspection.duration ? Math.floor(state.inspection.duration) : 0;
    for (const id of ["#sliderStart", "#sliderEnd"]) document.querySelector(id).max = duration;
    syncSlidersFromFields();
    try {
        await loadYouTubeApi();
    } catch (error) {
        note.textContent = error.message;
        return;
    }
    if (preview.player && preview.videoId === state.inspection.video_id) return;
    if (preview.player) preview.player.destroy();
    preview.videoId = state.inspection.video_id;
    note.textContent = "Lecteur YouTube prêt. Si la vidéo refuse la lecture intégrée, saisissez les heures à la main.";
    preview.player = new window.YT.Player("ytPlayer", {
        host: "https://www.youtube-nocookie.com",
        videoId: preview.videoId,
        playerVars: { rel: 0, playsinline: 1, origin: window.location.origin },
        events: {
            onError: () => {
                note.textContent = "Cette vidéo ne peut pas être lue ici (lecture intégrée refusée). Saisissez les heures à la main.";
            },
        },
    });
}

function closePreview() {
    document.querySelector("#previewPanel").classList.add("hidden");
    if (preview.player) {
        preview.player.destroy();
        preview.player = null;
        preview.videoId = null;
    }
}

function withPlayer(action) {
    if (preview.player && typeof preview.player.getCurrentTime === "function") action(preview.player);
}

function setupRangeControls() {
    document.querySelector("#togglePreview").addEventListener("click", () => {
        const panel = document.querySelector("#previewPanel");
        if (panel.classList.contains("hidden")) openPreview();
        else closePreview();
    });
    for (const id of ["#rangeStart", "#rangeEnd"]) {
        document.querySelector(id).addEventListener("input", () => {
            syncSlidersFromFields();
            updateRangeSummary();
        });
    }
    document.querySelector("#sliderStart").addEventListener("input", (event) => {
        const end = document.querySelector("#sliderEnd");
        if (Number(event.target.value) > Number(end.value) - 5) event.target.value = Math.max(0, Number(end.value) - 5);
        setRangeField("start", Number(event.target.value));
    });
    document.querySelector("#sliderEnd").addEventListener("input", (event) => {
        const start = document.querySelector("#sliderStart");
        if (Number(event.target.value) < Number(start.value) + 5) event.target.value = Number(start.value) + 5;
        setRangeField("end", Number(event.target.value));
    });
    document.querySelector("#sliderStart").addEventListener("change", (event) => {
        withPlayer((player) => player.seekTo(Number(event.target.value), true));
    });
    document.querySelector("#sliderEnd").addEventListener("change", (event) => {
        withPlayer((player) => player.seekTo(Math.max(0, Number(event.target.value) - 10), true));
    });
    document.querySelector("#setStartHere").addEventListener("click", () => {
        withPlayer((player) => setRangeField("start", player.getCurrentTime()));
    });
    document.querySelector("#setEndHere").addEventListener("click", () => {
        withPlayer((player) => setRangeField("end", player.getCurrentTime()));
    });
    document.querySelector("#playFromStart").addEventListener("click", () => {
        const start = parseClock(document.querySelector("#rangeStart").value);
        withPlayer((player) => {
            player.seekTo(Number.isFinite(start) && start ? start : 0, true);
            player.playVideo();
        });
    });
    document.querySelector("#playBeforeEnd").addEventListener("click", () => {
        const end = parseClock(document.querySelector("#rangeEnd").value);
        const duration = state.inspection && state.inspection.duration ? state.inspection.duration : 0;
        withPlayer((player) => {
            player.seekTo(Math.max(0, (Number.isFinite(end) && end ? end : duration) - 10), true);
            player.playVideo();
        });
    });
}

// ---- Distinction des intervenants : parties de 20 minutes au plus ----
const DIARIZE_MAX_CHUNK_MINUTES = 20;

function syncChunkOptions() {
    const diarize = document.querySelector("#diarize").checked;
    document.querySelector("#speakerLinking").disabled = !diarize;
    const select = document.querySelector("#chunkMinutes");
    for (const option of select.options) {
        const tooLong = Number(option.value) > DIARIZE_MAX_CHUNK_MINUTES;
        option.disabled = diarize && tooLong;
        if (tooLong) {
            const base = `${option.value} minutes`;
            option.textContent = diarize ? `${base} — sans distinction des intervenants` : base;
        }
    }
    if (diarize && Number(select.value) > DIARIZE_MAX_CHUNK_MINUTES) select.value = String(DIARIZE_MAX_CHUNK_MINUTES);
}

function resetInspection() {
    closePreview();
    document.querySelector("#rangeStart").value = "";
    document.querySelector("#rangeEnd").value = "";
    document.querySelector("#rangeSummary").textContent = "";
    state.needsConfirmation = false;
    state.inspection = null;
    document.querySelector("#inspectionCard").classList.add("hidden");
    document.querySelector("#confirmLong").checked = false;
    updateSubmitState();
}

function renderInspection(data) {
    state.inspection = data;
    document.querySelector("#inspectionTitle").textContent = data.title;
    document.querySelector("#inspectionDuration").textContent = formatReadableDuration(data.duration);
    document.querySelector("#inspectionChunks").textContent = data.estimated_chunks
        ? `${data.estimated_chunks} partie(s) prévue(s)`
        : "Nombre de parties inconnu";
    document.querySelector("#inspectionPricing").textContent = data.pricing_note;
    document.querySelector("#longConfirmation").classList.toggle("hidden", !data.requires_confirmation);
    state.needsConfirmation = data.requires_confirmation;
    document.querySelector("#inspectionCard").classList.remove("hidden");
    syncSlidersFromFields();
    updateRangeSummary();
    updateSubmitState();
}

// ---- Fenêtre des paramètres ----
let settingsAutoOpened = false;

function openSettings() {
    const dialog = document.querySelector("#settingsDialog");
    if (!dialog.open) dialog.showModal();
}

function openSettingsOnce() {
    if (settingsAutoOpened) return;
    settingsAutoOpened = true;
    openSettings();
}

function setupSettingsDialog() {
    const dialog = document.querySelector("#settingsDialog");
    document.querySelector("#openSettings").addEventListener("click", openSettings);
    document.querySelector("#closeSettings").addEventListener("click", () => dialog.close());
    // Un clic sur le fond assombri (en dehors du contenu) ferme la fenêtre.
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
    });
    document.querySelector("#health").addEventListener("click", () => {
        if (document.querySelector("#health").textContent.includes("Clé API")) openSettings();
    });
}

async function loadHealth() {
    const health = document.querySelector("#health");
    try {
        const data = await api("/api/health");
        const missing = Object.entries(data.dependencies).filter(([, ok]) => !ok).map(([name]) => name);
        if (missing.length) {
            health.textContent = `Manquant : ${missing.join(", ")}`;
            health.className = "health error";
        } else if (!data.api_key_configured) {
            health.textContent = "Clé API à configurer";
            health.className = "health";
            openSettingsOnce();
        } else {
            health.textContent = `Prêt · v${data.version}`;
            health.className = "health ok";
            document.querySelector("#settingsCard").classList.add("configured");
        }
    } catch (error) {
        health.textContent = "Serveur indisponible";
        health.className = "health error";
    }
}

async function loadYtdlpVersion() {
    try {
        const data = await api("/api/yt-dlp");
        document.querySelector("#ytdlpVersion").textContent = data.version || "non installé";
    } catch (error) {
        document.querySelector("#ytdlpVersion").textContent = "inconnue";
    }
}

async function loadJobs() {
    try {
        state.jobs = await api("/api/jobs");
        renderJobs();
        updateSubmitState();
        if (state.selectedJobId && !state.jobs.some((job) => job.id === state.selectedJobId)) closeSpeakerDetails();
    } catch (error) {
        document.querySelector("#jobs").innerHTML = `<p class="error-detail">${escapeHtml(error.message)}</p>`;
    }
}

function jobActions(job) {
    const actions = [];
    if (job.status === "completed") {
        for (const file of job.files || []) {
            actions.push(`<a class="download" href="/api/jobs/${job.id}/files/${encodeURIComponent(file.name)}">${escapeHtml(file.label)}</a>`);
        }
        if (job.diarize) actions.push(`<button class="review-button" type="button" data-review="${job.id}">Vérifier les intervenants</button>`);
        actions.push(`<button class="delete-button" type="button" data-delete="${job.id}">Supprimer</button>`);
    } else if (job.status === "failed") {
        actions.push(`<button class="review-button" type="button" data-retry="${job.id}">Reprendre</button>`);
        actions.push(`<button class="delete-button" type="button" data-delete="${job.id}">Supprimer</button>`);
    }
    return actions.join("");
}

function renderJobs() {
    const container = document.querySelector("#jobs");
    if (!state.jobs.length) {
        container.replaceChildren(document.querySelector("#emptyTemplate").content.cloneNode(true));
        return;
    }

    container.innerHTML = state.jobs.map((job) => {
        const isActive = activeStatuses.has(job.status);
        const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
        const actions = jobActions(job);
        return `
            <article class="job">
                <div class="job-top">
                    <div>
                        <h3>${escapeHtml(job.title || "Vidéo en cours d’analyse")}</h3>
                        <div class="job-url" title="${escapeHtml(job.url)}">${escapeHtml(job.url)}</div>
                    </div>
                    <span class="badge ${escapeHtml(job.status)}">${escapeHtml(statusLabels[job.status] || job.status)}</span>
                </div>
                <div class="progress-heading"><span>Progression</span><strong>${progress} %</strong></div>
                <div class="progress-track" role="progressbar" aria-label="Progression" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}">
                    <div class="progress-bar${isActive ? " active" : ""}" style="width:${progress}%"></div>
                </div>
                <div class="job-status-line">
                    <p class="job-message">${escapeHtml(job.message)}</p>
                    <div class="job-times">
                        <span class="job-timer" data-job-timer data-status="${escapeHtml(job.status)}" data-created-at="${escapeHtml(job.created_at)}" data-updated-at="${escapeHtml(job.updated_at)}" data-completed-at="${escapeHtml(job.completed_at)}"></span>
                        <span class="job-eta" data-job-eta="${escapeHtml(job.id)}"></span>
                    </div>
                </div>
                ${job.warning ? `<div class="warning-detail">${escapeHtml(job.warning)}</div>` : ""}
                ${job.status === "failed" ? `<div class="error-detail">${escapeHtml(job.message)}</div>` : ""}
                ${actions ? `<div class="job-actions">${actions}</div>` : ""}
            </article>`;
    }).join("");

    container.querySelectorAll("[data-review]").forEach((button) => {
        button.addEventListener("click", () => openSpeakerDetails(button.dataset.review));
    });
    container.querySelectorAll("[data-retry]").forEach((button) => {
        button.addEventListener("click", () => retryJob(button.dataset.retry, button));
    });
    container.querySelectorAll("[data-delete]").forEach((button) => {
        button.addEventListener("click", () => deleteJob(button.dataset.delete, button));
    });
    updateLiveCounters();
}

function formatClock(seconds) {
    return formatDuration(seconds);
}

function renderSpeakerRows(speakers) {
    const fields = document.querySelector("#speakerFields");
    fields.innerHTML = speakers.map((speaker, index) => {
        const percent = (speaker.share * 100).toFixed(speaker.share >= 0.1 ? 0 : 1);
        const minor = speaker.share < 0.01;
        return `
            <div class="speaker-row${minor ? " minor" : ""}" data-label="${escapeHtml(speaker.label)}">
                <div>
                    <code>${index + 1}. ${escapeHtml(speaker.label)}</code>
                    <div class="speaker-meta">${percent} % du texte · ${speaker.segments} phrases · dès ${formatClock(speaker.first_start)}${speaker.parts > 1 ? ` · ${speaker.parts} parties` : ""}</div>
                </div>
                <div class="speaker-sample">${speaker.sample ? `« ${escapeHtml(speaker.sample)} »` : ""}</div>
                <div>
                    <input type="text" name="${escapeHtml(speaker.label)}" value="${escapeHtml(speaker.name)}" maxlength="100" aria-label="Nom pour ${escapeHtml(speaker.label)}">
                    <span class="speaker-badge-slot"></span>
                </div>
            </div>`;
    }).join("");
    applyMinorFilter();
}

function applyMinorFilter() {
    const hide = document.querySelector("#hideMinor").checked;
    document.querySelectorAll(".speaker-row.minor").forEach((row) => row.classList.toggle("is-hidden", hide));
}

async function openSpeakerDetails(jobId) {
    try {
        const data = await api(`/api/jobs/${jobId}/speakers`);
        state.selectedJobId = jobId;
        const card = document.querySelector("#detailsCard");
        document.querySelector("#detailsTitle").textContent = data.title || "Intervenants";
        document.querySelector("#suggestMessage").textContent = "";
        document.querySelector("#speakerMessage").textContent = "";
        renderSpeakerRows(data.speakers);
        card.classList.remove("hidden");
        card.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
        window.alert(error.message);
    }
}

async function analyzeNames() {
    const jobId = state.selectedJobId;
    if (!jobId) return;
    const button = document.querySelector("#analyzeNames");
    const message = document.querySelector("#suggestMessage");
    button.disabled = true;
    message.className = "form-message";
    message.textContent = "Analyse des noms cités…";
    try {
        const result = await api(`/api/jobs/${jobId}/names`, { method: "POST" });
        const stats = result.stats;
        message.textContent = `${stats.mentions} noms cités : ${stats.reconnues} corrigés automatiquement, `
            + `${stats.a_verifier} à vérifier, ${stats.inconnues} non reconnus`
            + (stats.titres ? `, ${stats.titres} titres « l'échevin(e) » ignorés` : "")
            + ` (liste de ${stats.elus_en_exercice} élus en exercice en ${stats.annee_reference}). `
            + "Le fichier « Noms cités (CSV) » est disponible dans la liste des traitements.";
        await loadJobs();
    } catch (error) {
        message.className = "form-message error";
        message.textContent = error.message;
    } finally {
        button.disabled = false;
    }
}

async function suggestNames() {
    const jobId = state.selectedJobId;
    if (!jobId) return;
    if (!window.confirm(
        "Les formules de passage de parole (« Madame X, vous avez la parole ») sont analysées sur votre ordinateur. "
        + "Pour les autres voix, de courts extraits de la transcription (texte uniquement, pas l'audio) peuvent être "
        + "envoyés à OpenAI, ce qui consomme un peu de crédit API. Continuer ?"
    )) return;
    const button = document.querySelector("#suggestNames");
    const message = document.querySelector("#suggestMessage");
    button.disabled = true;
    message.className = "form-message";
    message.textContent = "Analyse en cours… cela peut prendre une minute.";
    try {
        const result = await api(`/api/jobs/${jobId}/speakers/suggest`, { method: "POST" });
        let applied = 0;
        for (const suggestion of result.suggestions) {
            const row = [...document.querySelectorAll(".speaker-row")].find((item) => item.dataset.label === suggestion.label);
            if (!row) continue;
            const input = row.querySelector("input");
            if (input.value !== suggestion.label) continue; // ne jamais écraser un nom déjà saisi
            input.value = suggestion.name;
            const slot = row.querySelector(".speaker-badge-slot");
            const source = suggestion.source ? ` (${suggestion.source})` : "";
            slot.innerHTML = `<span class="speaker-badge ${escapeHtml(suggestion.confidence)}" title="${escapeHtml(suggestion.evidence)}">Suggestion ${escapeHtml(suggestion.confidence)}${escapeHtml(source)} · ${escapeHtml(suggestion.evidence)}</span>`;
            applied += 1;
        }
        message.textContent = `${applied} suggestion(s) proposée(s) dont ${result.by_rules || 0} par règles`
            + (result.analyzed_labels ? `, ${result.analyzed_labels} voix soumises à l'IA` : "")
            + (result.skipped_labels ? ` (${result.skipped_labels} voix très courtes ignorées)` : "")
            + (result.note ? ` — ${result.note}` : "")
            + ". Ce sont des hypothèses : vérifiez-les, puis cliquez sur « Enregistrer les noms ».";
    } catch (error) {
        message.className = "form-message error";
        message.textContent = error.message;
    } finally {
        button.disabled = false;
    }
}

function closeSpeakerDetails() {
    state.selectedJobId = null;
    document.querySelector("#detailsCard").classList.add("hidden");
}

async function retryJob(jobId, button) {
    button.disabled = true;
    try {
        await api(`/api/jobs/${jobId}/retry`, { method: "POST" });
        await loadJobs();
    } catch (error) {
        window.alert(error.message);
    } finally {
        button.disabled = false;
    }
}

async function deleteJob(jobId, button) {
    if (!window.confirm("Supprimer ce traitement et tous ses fichiers locaux ?")) return;
    button.disabled = true;
    try {
        await api(`/api/jobs/${jobId}`, { method: "DELETE" });
        if (state.selectedJobId === jobId) closeSpeakerDetails();
        await loadJobs();
    } catch (error) {
        window.alert(error.message);
        button.disabled = false;
    }
}

document.querySelector("#keyForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    const message = document.querySelector("#keyMessage");
    button.disabled = true;
    message.className = "form-message";
    message.textContent = "Enregistrement…";
    try {
        await api("/api/settings/api-key", {
            method: "POST",
            body: JSON.stringify({ api_key: document.querySelector("#apiKey").value }),
        });
        document.querySelector("#apiKey").value = "";
        message.textContent = "Clé enregistrée sur cet ordinateur.";
        await loadHealth();
    } catch (error) {
        message.className = "form-message error";
        message.textContent = error.message;
    } finally {
        button.disabled = false;
    }
});

document.querySelector("#jobForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = document.querySelector("#jobMessage");
    const url = document.querySelector("#videoUrl").value;
    const chunkMinutes = Number(document.querySelector("#chunkMinutes").value);
    const cookieBrowser = document.querySelector("#cookieBrowser").value || null;
    state.submitting = true;
    updateSubmitState();
    message.className = "form-message";

    try {
        if (!state.inspection) {
            message.textContent = "Analyse de la durée et du titre…";
            const inspection = await api("/api/videos/inspect", {
                method: "POST",
                body: JSON.stringify({
                    url,
                    chunk_minutes: chunkMinutes,
                    cookie_browser: cookieBrowser,
                    diarize: document.querySelector("#diarize").checked,
                }),
            });
            renderInspection(inspection);
            message.textContent = "Vérifiez les informations, puis confirmez le démarrage.";
            return;
        }

        const range = currentRange();
        const problem = rangeProblem(range);
        if (problem) throw new Error(problem);
        if (state.needsConfirmation && !document.querySelector("#confirmLong").checked) {
            throw new Error("Cochez la confirmation pour cette vidéo longue ou de durée inconnue.");
        }
        message.textContent = "Ajout à la file d’attente…";
        await api("/api/jobs", {
            method: "POST",
            body: JSON.stringify({
                url,
                inspection_id: state.inspection.inspection_id,
                confirm_long_video: document.querySelector("#confirmLong").checked,
                diarize: document.querySelector("#diarize").checked,
                language: document.querySelector("#language").value || null,
                speaker_linking: document.querySelector("#diarize").checked ? document.querySelector("#speakerLinking").value : "off",
                chunk_minutes: chunkMinutes,
                api_concurrency: Number(document.querySelector("#apiConcurrency").value),
                cookie_browser: cookieBrowser,
                start_seconds: range.start,
                end_seconds: range.end,
            }),
        });
        document.querySelector("#videoUrl").value = "";
        resetInspection();
        message.textContent = "Traitement démarré. Vous pouvez fermer cet onglet.";
        await loadJobs();
    } catch (error) {
        message.className = "form-message error";
        message.textContent = error.message;
    } finally {
        state.submitting = false;
        updateSubmitState();
    }
});

document.querySelector("#diarize").addEventListener("change", () => {
    syncChunkOptions();
    resetInspection();
});
syncChunkOptions();

for (const id of ["videoUrl", "chunkMinutes", "cookieBrowser"]) {
    document.querySelector(`#${id}`).addEventListener("input", resetInspection);
}

document.querySelector("#speakersForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const jobId = state.selectedJobId;
    if (!jobId) return;
    const names = Object.fromEntries(new FormData(event.currentTarget).entries());
    const message = document.querySelector("#speakerMessage");
    message.textContent = "Mise à jour des fichiers…";
    try {
        await api(`/api/jobs/${jobId}/speakers`, {
            method: "POST",
            body: JSON.stringify({ names }),
        });
        message.className = "form-message";
        message.textContent = "Noms et fichiers mis à jour.";
        await loadJobs();
    } catch (error) {
        message.className = "form-message error";
        message.textContent = error.message;
    }
});

document.querySelector("#updateYtdlp").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const message = document.querySelector("#ytdlpMessage");
    button.disabled = true;
    message.className = "form-message";
    message.textContent = "Mise à jour en cours… cela peut prendre une minute.";
    try {
        const includeDev = document.querySelector("#ytdlpDev").checked;
        const result = await api("/api/yt-dlp/update", {
            method: "POST",
            body: JSON.stringify({ include_dev: includeDev }),
        });
        const kind = result.development_build ? " — version de développement" : "";
        message.textContent = result.updated
            ? `yt-dlp mis à jour : ${result.previous_version || "?"} → ${result.version}${kind}.`
            : `yt-dlp est déjà à jour (${result.version}${kind}).`
                + (includeDev ? "" : " Cochez « versions de développement » pour chercher une version plus récente.");
        await loadYtdlpVersion();
    } catch (error) {
        message.className = "form-message error";
        message.textContent = error.message;
    } finally {
        button.disabled = false;
    }
});

setupRangeControls();
setupSettingsDialog();
document.querySelector("#suggestNames").addEventListener("click", suggestNames);
document.querySelector("#analyzeNames").addEventListener("click", analyzeNames);
document.querySelector("#hideMinor").addEventListener("change", applyMinorFilter);
document.querySelector("#closeDetails").addEventListener("click", closeSpeakerDetails);
document.querySelector("#refreshJobs").addEventListener("click", loadJobs);

async function initialize() {
    await Promise.all([loadHealth(), loadJobs(), loadYtdlpVersion()]);
    state.polling = window.setInterval(loadJobs, 3000);
    state.clock = window.setInterval(updateLiveCounters, 1000);
}

initialize();
