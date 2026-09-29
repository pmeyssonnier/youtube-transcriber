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

function resetInspection() {
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
    document.querySelector("#inspectionCard").classList.remove("hidden");
    updateSubmitState();
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

async function openSpeakerDetails(jobId) {
    try {
        const job = await api(`/api/jobs/${jobId}`);
        if (job.status !== "completed") return;
        state.selectedJobId = job.id;
        const card = document.querySelector("#detailsCard");
        const fields = document.querySelector("#speakerFields");
        document.querySelector("#detailsTitle").textContent = job.title || "Intervenants";
        fields.innerHTML = Object.entries(job.speaker_names || {}).map(([raw, name]) => `
            <label class="speaker-field">
                <code>${escapeHtml(raw)}</code>
                <input type="text" name="${escapeHtml(raw)}" value="${escapeHtml(name)}" maxlength="100">
            </label>
        `).join("");
        card.classList.remove("hidden");
        card.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
        window.alert(error.message);
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
                body: JSON.stringify({ url, chunk_minutes: chunkMinutes, cookie_browser: cookieBrowser }),
            });
            renderInspection(inspection);
            message.textContent = "Vérifiez les informations, puis confirmez le démarrage.";
            return;
        }

        if (state.inspection.requires_confirmation && !document.querySelector("#confirmLong").checked) {
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
                chunk_minutes: chunkMinutes,
                api_concurrency: Number(document.querySelector("#apiConcurrency").value),
                cookie_browser: cookieBrowser,
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
        const result = await api("/api/yt-dlp/update", { method: "POST" });
        message.textContent = result.updated
            ? `yt-dlp mis à jour : ${result.previous_version || "?"} → ${result.version}.`
            : `yt-dlp est déjà à jour (${result.version}).`;
        await loadYtdlpVersion();
    } catch (error) {
        message.className = "form-message error";
        message.textContent = error.message;
    } finally {
        button.disabled = false;
    }
});

document.querySelector("#closeDetails").addEventListener("click", closeSpeakerDetails);
document.querySelector("#refreshJobs").addEventListener("click", loadJobs);

async function initialize() {
    await Promise.all([loadHealth(), loadJobs(), loadYtdlpVersion()]);
    state.polling = window.setInterval(loadJobs, 3000);
    state.clock = window.setInterval(updateLiveCounters, 1000);
}

initialize();
