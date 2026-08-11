/**
 * Call recordings browser.
 *
 * Lists what the `recordings` table holds and plays or transcribes one at a time. The store
 * has had rows since the recorder started registering them; this is the first view onto them.
 *
 * Laid out as one card per recording, matching the call queues page: a single-row header
 * carrying identity, state and labelled stats, over a body that collapses as one unit.
 *
 * Three rules from the API are mirrored here so the UI does not fight them:
 *
 *   - Transcript text is never in a list response. The card shows a preview only after its
 *     transcript has been fetched, and that fetch is audited per recording.
 *   - Audio and text expire on separate clocks. A row whose audio is gone still lists, still
 *     reads, and says so -- it is not an error and must not look like one.
 *   - Voicemail is off by default. It lives in the same table but has its own page.
 */

import { getAuthHeaders, getApiBaseUrl } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
import { escapeHtml } from '../utils/html.ts';

interface Recording {
    id: number;
    session_id: string | null;
    call_id: string | null;
    kind: string;
    duration_seconds: number | null;
    participants: string[] | null;
    started_at: string | null;
    ended_at: string | null;
    audio_deleted_at: string | null;
    created_at: string | null;
}

interface RecordingListResponse {
    recordings?: Recording[];
    count?: number;
}

/** One speaker turn. `speaker` is empty for single-source audio such as voicemail. */
interface TranscriptLine {
    speaker: string;
    text: string;
    start: number | null;
    end: number | null;
}

interface TranscriptRun {
    id: number;
    provider: string | null;
    model: string | null;
    language: string | null;
    confidence: number | null;
    text?: string | null;
    lines?: TranscriptLine[];
    created_at: string | null;
}

interface TranscriptResponse {
    recording_id?: number;
    transcripts?: TranscriptRun[];
}

/** Recordings currently rendered, by id, so actions do not re-fetch the list. */
const recordings = new Map<number, Recording>();

/** Transcripts already fetched, by recording id. Each fetch is an audited read. */
const transcripts = new Map<number, TranscriptRun[]>();

/** Recordings whose body is expanded. Ids, so re-rendering keeps the open ones open. */
const expanded = new Set<number>();

/** Recordings whose transcript is showing in full rather than as a preview. */
const fullTranscript = new Set<number>();

function failureMessage(response: Response, action: string): string {
    if (response.status === 403) return `Not authorized to ${action}`;
    if (response.status === 410) return 'Audio has been deleted under the retention policy';
    if (response.status === 415) return 'Audio is in a format the browser cannot play';
    if (response.status === 422) return 'Audio file is not a readable WAV';
    if (response.status === 503) return 'Recording storage is unavailable';
    return `Could not ${action} (HTTP ${response.status})`;
}

function formatDuration(seconds: number | null): string {
    if (seconds === null || seconds === undefined) return 'Unknown';
    const total = Math.round(seconds);
    const mins = Math.floor(total / 60);
    const secs = total % 60;
    return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
}

/** mm:ss for a position inside a recording, as transcript lines are stamped. */
function formatOffset(seconds: number | null): string {
    if (seconds === null || seconds === undefined) return '';
    const total = Math.max(0, Math.round(seconds));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function formatTimestamp(value: string | null): string {
    if (!value) return 'Unknown';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? 'Unknown' : parsed.toLocaleString();
}

function participantLabel(rec: Recording): string {
    const parts = (rec.participants ?? []).filter(Boolean).map((p) => String(p));
    if (parts.length === 0) return `Recording ${rec.id}`;
    return parts.map((p) => escapeHtml(p)).join(' &harr; ');
}

// --- Loading ---------------------------------------------------------------

export async function loadCallRecordings(): Promise<void> {
    const container = document.getElementById('call-recordings-list');
    if (!container) return;

    const includeVoicemail = (document.getElementById('recordings-include-voicemail') as HTMLInputElement | null)?.checked;
    const participant = (document.getElementById('recordings-participant-filter') as HTMLInputElement | null)?.value.trim();

    const params = new URLSearchParams({ limit: '200' });
    if (includeVoicemail) params.set('include_voicemail', '1');
    if (participant) params.set('participant', participant);

    container.innerHTML = '<div class="queues-cards"><div class="loading">Loading recordings...</div></div>';

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/recordings?${params.toString()}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            // A 403 here is the participant_access default, not a fault. Say which, or an
            // admin goes looking for a broken feature that is actually a setting.
            container.innerHTML = `<div class="queues-empty">${escapeHtml(
                response.status === 403
                    ? 'You do not have access to call recordings. Recordings are admin-only unless participant access is enabled.'
                    : failureMessage(response, 'load recordings')
            )}</div>`;
            return;
        }

        const data: RecordingListResponse = await response.json();
        recordings.clear();
        for (const rec of data.recordings ?? []) recordings.set(rec.id, rec);
        renderRecordings();
    } catch (error: unknown) {
        console.error('Error loading recordings:', error);
        container.innerHTML = '<div class="queues-empty">Failed to load recordings</div>';
        showNotification('Failed to load recordings', 'error');
    }
}

// --- Rendering -------------------------------------------------------------

function renderRecordings(): void {
    const container = document.getElementById('call-recordings-list');
    if (!container) return;

    if (recordings.size === 0) {
        const filtered = (document.getElementById('recordings-participant-filter') as HTMLInputElement | null)?.value.trim();
        container.innerHTML = `<div class="queues-empty">${
            filtered ? `No recordings involving ${escapeHtml(filtered)}` : 'No recordings'
        }</div>`;
        return;
    }

    container.innerHTML = `<div class="queues-cards">${
        [...recordings.values()].map(renderRecordingCard).join('')
    }</div>`;
}

function renderRecordingCard(rec: Recording): string {
    const open = expanded.has(rec.id);
    const audioGone = Boolean(rec.audio_deleted_at);

    const statePill = audioGone
        ? `<span class="en-pill off" title="Removed by the retention policy"><span class="en-dot"></span>Audio expired</span>`
        : `<span class="en-pill on"><span class="en-dot"></span>Audio available</span>`;

    const mediaActions = audioGone
        ? ''
        : `<button type="button" class="qbtn" data-rec-play="${rec.id}">Play</button>
           <button type="button" class="qbtn" data-rec-download="${rec.id}">Download</button>`;

    const head = `
        <div class="queue-card-head">
            <span class="qch-title" role="button" aria-expanded="${open}" data-rec-toggle="${rec.id}">
                <span class="queue-chevron${open ? ' open' : ''}" aria-hidden="true">&#9654;</span>
                <strong>${participantLabel(rec)}</strong>
            </span>
            ${statePill}
            <span class="qch-stats">
                <span class="qch-stat"><span class="k">Kind</span><span class="v">${escapeHtml(rec.kind)}</span></span>
                <span class="qch-stat"><span class="k">Duration</span><span class="v">${formatDuration(rec.duration_seconds)}</span></span>
                <span class="qch-stat"><span class="k">Started</span><span class="v">${formatTimestamp(rec.started_at ?? rec.created_at)}</span></span>
            </span>
            <span class="qch-actions">${mediaActions}</span>
        </div>
    `;

    return `
        <div class="queue-card">
            ${head}
            <div class="queue-card-body${open ? '' : ' collapsed'}" data-rec-body="${rec.id}">
                ${open ? renderCardBody(rec) : ''}
            </div>
        </div>
    `;
}

/** Built only while expanded, so a long list does not carry every player and transcript. */
function renderCardBody(rec: Recording): string {
    // A slot, not an <audio> element. Rendering `<audio controls>` before its source exists
    // shows a fully working-looking player whose own play button reports "no supported source
    // was found" -- the browser's message for an empty element, indistinguishable from a
    // codec failure. The player is inserted by attachAudio() once it has something to play.
    const player = rec.audio_deleted_at
        ? '<div class="rec-transcript-none">Audio was removed by the retention policy.</div>'
        : `<div class="rec-player-slot" data-rec-player="${rec.id}">
               <div class="rec-transcript-none">Loading audio...</div>
           </div>`;

    return `
        <div class="rec-body">
            ${player}
            <div class="rec-transcript" data-rec-transcript="${rec.id}">
                ${renderTranscript(rec.id)}
            </div>
        </div>
    `;
}

function renderTranscript(recordingId: number): string {
    const runs = transcripts.get(recordingId);

    if (runs === undefined) {
        return `<button type="button" class="qbtn" data-rec-fetch-transcript="${recordingId}">Show transcript</button>`;
    }
    if (runs.length === 0) {
        return `<div class="rec-transcript-none">No transcript for this recording.</div>`;
    }

    const showFull = fullTranscript.has(recordingId);
    return runs.map((run) => renderTranscriptRun(recordingId, run, showFull)).join('');
}

function renderTranscriptRun(recordingId: number, run: TranscriptRun, showFull: boolean): string {
    const lines = run.lines ?? [];
    const flat = (run.text ?? '').trim();

    // Whisper genuinely cannot report a confidence. Rendering null as 0% asserts the
    // transcript is worthless, which is a different claim from "not measured".
    const confidence = run.confidence === null || run.confidence === undefined
        ? 'not reported'
        : `${Math.round(run.confidence * 100)}%`;

    const meta = [
        run.provider ? escapeHtml(run.provider) : null,
        run.model ? escapeHtml(run.model) : null,
        lines.length > 0 ? `${lines.length} line${lines.length === 1 ? '' : 's'}` : null,
        `accuracy ${confidence}`
    ].filter(Boolean).join(' &middot; ');

    let content: string;
    if (lines.length === 0) {
        // Timing was never recorded, so there are no turns to lay out -- show the prose.
        content = flat
            ? `<p class="rec-transcript-flat">${escapeHtml(flat)}</p>`
            : `<div class="rec-transcript-none">Transcript is empty.</div>`;
    } else if (showFull) {
        content = `<div class="rec-lines">${lines.map(renderLine).join('')}</div>`;
    } else {
        const preview = flat || lines.map((l) => l.text).join(' ');
        content = `<p class="rec-transcript-preview">${escapeHtml(preview)}</p>`;
    }

    const toggle = lines.length > 0
        ? `<button type="button" class="qbtn" data-rec-expand-transcript="${recordingId}">
               ${showFull ? 'Show less' : 'Show full transcript'}
           </button>`
        : '';

    return `
        <div class="rec-transcript-run">
            <div class="rec-transcript-head">
                <span class="k">Transcript</span>
                <span class="rec-transcript-meta">${meta}</span>
            </div>
            ${content}
            ${toggle}
        </div>
    `;
}

function renderLine(line: TranscriptLine): string {
    const offset = formatOffset(line.start);
    // Voicemail has one speaker by definition, so the column is omitted rather than shown
    // empty -- an blank label reads as missing data instead of "not applicable".
    const speaker = line.speaker
        ? `<span class="rec-line-speaker">${escapeHtml(line.speaker)}</span>`
        : '';

    return `
        <div class="rec-line">
            <span class="rec-line-time">${offset}</span>
            ${speaker}
            <span class="rec-line-text">${escapeHtml(line.text)}</span>
        </div>
    `;
}

// --- Actions ---------------------------------------------------------------

async function toggleCard(recordingId: number): Promise<void> {
    const rec = recordings.get(recordingId);
    if (!rec) return;

    const open = !expanded.has(recordingId);
    if (open) expanded.add(recordingId);
    else expanded.delete(recordingId);

    const body = document.querySelector(`[data-rec-body="${recordingId}"]`) as HTMLElement | null;
    if (body) {
        if (!open) {
            // Release the blob before the element goes; otherwise it is held until reload.
            const player = body.querySelector('audio') as HTMLAudioElement | null;
            if (player?.dataset.objectUrl) URL.revokeObjectURL(player.dataset.objectUrl);
        }
        body.innerHTML = open ? renderCardBody(rec) : '';
        body.classList.toggle('collapsed', !open);
    }
    const toggle = document.querySelector(`[data-rec-toggle="${recordingId}"]`);
    if (toggle) {
        toggle.setAttribute('aria-expanded', String(open));
        toggle.querySelector('.queue-chevron')?.classList.toggle('open', open);
    }

    // Loaded on expand rather than on a Play click, so the controls the user is now looking
    // at are backed by a source the moment they appear.
    if (open && !rec.audio_deleted_at) await attachAudio(recordingId);
}

/**
 * Fetch a recording's audio as a blob URL.
 *
 * The endpoint authenticates with a bearer token, and an <audio src> pointed at it makes the
 * browser issue its own request, which carries no Authorization header and is rejected.
 * Fetching here carries the header and yields a URL the browser will load. The caller must
 * revoke it, or the blob is held for the life of the page.
 */
async function fetchRecordingAudio(recordingId: number): Promise<string> {
    const API_BASE = getApiBaseUrl();
    const response = await fetch(`${API_BASE}/api/recordings/${recordingId}/audio`, {
        headers: getAuthHeaders()
    });

    if (!response.ok) throw new Error(failureMessage(response, 'play this recording'));
    return URL.createObjectURL(await response.blob());
}

/**
 * Fetch the audio and put a real player in the slot.
 *
 * On failure the slot gets the server's reason -- 415 for a codec we cannot convert, 410 for
 * expired audio -- rather than an empty player. An `<audio>` with no source reports only "no
 * supported source was found" whatever went wrong, which hides the actual cause.
 *
 * Returns the player once it has a source, or null when there is nothing to play.
 */
async function attachAudio(recordingId: number): Promise<HTMLAudioElement | null> {
    const slot = document.querySelector(`[data-rec-player="${recordingId}"]`) as HTMLElement | null;
    if (!slot) return null;

    const existing = slot.querySelector('audio') as HTMLAudioElement | null;
    if (existing?.dataset.objectUrl) return existing;

    try {
        const objectUrl = await fetchRecordingAudio(recordingId);
        const player = document.createElement('audio');
        player.className = 'rec-player';
        player.controls = true;
        player.src = objectUrl;
        player.dataset.objectUrl = objectUrl;

        // The element is only inserted once it has a source, so the controls the user sees
        // are always backed by something playable.
        slot.replaceChildren(player);
        return player;
    } catch (error: unknown) {
        console.error('Error loading recording audio:', error);
        const message = error instanceof Error ? error.message : 'Failed to load audio';
        slot.replaceChildren();
        const note = document.createElement('div');
        note.className = 'rec-transcript-none';
        note.textContent = message;
        slot.appendChild(note);
        return null;
    }
}

async function playRecording(recordingId: number): Promise<void> {
    if (!expanded.has(recordingId)) {
        // Expanding kicks off attachAudio itself; await it so the player exists below.
        await toggleCard(recordingId);
    }

    const player = await attachAudio(recordingId);
    if (!player) return;

    try {
        // Awaited so a decode failure surfaces here rather than as an unhandled rejection.
        await player.play();
    } catch (error: unknown) {
        console.error('Error playing recording:', error);
        showNotification(error instanceof Error ? error.message : 'Failed to play recording', 'error');
    }
}

async function downloadRecording(recordingId: number): Promise<void> {
    let objectUrl: string | null = null;
    try {
        objectUrl = await fetchRecordingAudio(recordingId);
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = `recording_${recordingId}.wav`;
        document.body.appendChild(link);
        link.click();
        link.remove();
    } catch (error: unknown) {
        console.error('Error downloading recording:', error);
        showNotification(error instanceof Error ? error.message : 'Failed to download recording', 'error');
    } finally {
        // Safe to revoke immediately: the browser already holds its own reference.
        if (objectUrl) URL.revokeObjectURL(objectUrl);
    }
}

async function fetchTranscript(recordingId: number): Promise<void> {
    const target = document.querySelector(`[data-rec-transcript="${recordingId}"]`);
    if (target) target.innerHTML = '<div class="rec-transcript-none">Loading transcript...</div>';

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/recordings/${recordingId}/transcript`, {
            headers: getAuthHeaders()
        });

        if (response.status === 404) {
            transcripts.set(recordingId, []);
        } else if (!response.ok) {
            if (target) {
                target.innerHTML = `<div class="rec-transcript-none">${escapeHtml(failureMessage(response, 'load this transcript'))}</div>`;
            }
            return;
        } else {
            const data: TranscriptResponse = await response.json();
            transcripts.set(recordingId, data.transcripts ?? []);
        }
    } catch (error: unknown) {
        console.error('Error loading transcript:', error);
        if (target) target.innerHTML = '<div class="rec-transcript-none">Failed to load transcript</div>';
        return;
    }

    if (target) target.innerHTML = renderTranscript(recordingId);
}

function toggleTranscriptLength(recordingId: number): void {
    if (fullTranscript.has(recordingId)) fullTranscript.delete(recordingId);
    else fullTranscript.add(recordingId);

    const target = document.querySelector(`[data-rec-transcript="${recordingId}"]`);
    if (target) target.innerHTML = renderTranscript(recordingId);
}

// --- Wiring ----------------------------------------------------------------

/**
 * One delegated listener for the whole list.
 *
 * Bound to the container rather than emitted as inline onclick attributes: the handlers take
 * a numeric id that would otherwise be interpolated into markup, and the CSP work here is
 * trying to retire 'unsafe-inline' rather than add to it.
 */
function bindActions(): void {
    const container = document.getElementById('call-recordings-list');
    if (!container || container.dataset.bound === 'true') return;
    container.dataset.bound = 'true';

    container.addEventListener('click', (event) => {
        const el = (event.target as HTMLElement | null)?.closest('[data-rec-toggle], button');
        if (!el) return;
        const data = (el as HTMLElement).dataset;

        if (data.recToggle) void toggleCard(Number(data.recToggle));
        else if (data.recPlay) void playRecording(Number(data.recPlay));
        else if (data.recDownload) void downloadRecording(Number(data.recDownload));
        else if (data.recFetchTranscript) void fetchTranscript(Number(data.recFetchTranscript));
        else if (data.recExpandTranscript) toggleTranscriptLength(Number(data.recExpandTranscript));
    });
}

let filterTimer: ReturnType<typeof setTimeout> | undefined;

export function initCallRecordings(): void {
    bindActions();

    const voicemail = document.getElementById('recordings-include-voicemail');
    if (voicemail && voicemail.dataset.bound !== 'true') {
        voicemail.dataset.bound = 'true';
        voicemail.addEventListener('change', () => void loadCallRecordings());
    }

    const participant = document.getElementById('recordings-participant-filter');
    if (participant && participant.dataset.bound !== 'true') {
        participant.dataset.bound = 'true';
        // Debounced: the filter runs server-side, and a query per keystroke would be one
        // audited list request per character typed.
        participant.addEventListener('input', () => {
            clearTimeout(filterTimer);
            filterTimer = setTimeout(() => void loadCallRecordings(), 300);
        });
    }

    void loadCallRecordings();
}

window.initCallRecordings = initCallRecordings;
window.loadCallRecordings = loadCallRecordings;
