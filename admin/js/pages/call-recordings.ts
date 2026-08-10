/**
 * Call recordings browser.
 *
 * Lists what the `recordings` table holds -- call audio, voicemail and conference alike --
 * and plays or transcribes one at a time. The store has had rows since the recorder started
 * registering them; this is the first view onto them.
 *
 * Two rules from the API are mirrored here so the UI does not fight them:
 *
 *   - Transcript text is never in a list response. The list shows only whether a transcript
 *     exists; the text arrives from a per-recording fetch that the server audits.
 *   - Audio and text expire on separate clocks. A row whose audio is gone still shows, still
 *     reads, and says so -- it is not an error and must not look like one.
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

interface TranscriptRun {
    id: number;
    source: string | null;
    provider: string | null;
    model: string | null;
    language: string | null;
    confidence: number | null;
    text?: string | null;
    segments?: unknown;
    created_at: string | null;
}

interface TranscriptResponse {
    recording_id?: number;
    transcripts?: TranscriptRun[];
}

/** Reported per read so a failure names the recording rather than the page. */
function reportFailure(response: Response, action: string): string {
    if (response.status === 403) return `Not authorized to ${action}`;
    if (response.status === 410) return 'Audio has been deleted under the retention policy';
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

function formatTimestamp(value: string | null): string {
    if (!value) return 'Unknown';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? 'Unknown' : parsed.toLocaleString();
}

export async function loadCallRecordings(): Promise<void> {
    const container = document.getElementById('call-recordings-list');
    if (!container) return;

    const kind = (document.getElementById('recordings-kind-filter') as HTMLSelectElement | null)?.value ?? '';
    const params = new URLSearchParams({ limit: '100' });
    if (kind) params.set('kind', kind);

    container.innerHTML = '<div class="info-box">Loading recordings...</div>';

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/recordings?${params.toString()}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            // A 403 here is the participant_access default, not a bug. Say which, or an
            // admin turns the feature off looking for a fault that is a setting.
            container.innerHTML = `<div class="info-box">${escapeHtml(
                response.status === 403
                    ? 'You do not have access to call recordings. Recordings are admin-only unless participant access is enabled.'
                    : reportFailure(response, 'load recordings')
            )}</div>`;
            return;
        }

        const data: RecordingListResponse = await response.json();
        renderRecordings(data.recordings ?? []);
    } catch (error: unknown) {
        console.error('Error loading recordings:', error);
        container.innerHTML = '<div class="info-box">Failed to load recordings</div>';
        showNotification('Failed to load recordings', 'error');
    }
}

function renderRecordings(recordings: Recording[]): void {
    const container = document.getElementById('call-recordings-list');
    if (!container) return;

    if (recordings.length === 0) {
        container.innerHTML = '<div class="info-box">No recordings</div>';
        return;
    }

    const rows = recordings.map((rec) => {
        const audioGone = Boolean(rec.audio_deleted_at);
        const participants = (rec.participants ?? []).map((p) => escapeHtml(String(p))).join(', ') || '—';

        // Audio expired is a normal end state, not a failure: the transcript usually
        // outlives it, so the row keeps its transcript button and loses only playback.
        const playButton = audioGone
            ? '<span class="muted" title="Removed by the retention policy">Audio expired</span>'
            : `<button class="btn btn-primary btn-sm" data-recording-play="${rec.id}">Play</button>
               <button class="btn btn-secondary btn-sm" data-recording-download="${rec.id}">Download</button>`;

        return `
            <tr>
                <td>${rec.id}</td>
                <td>${escapeHtml(rec.kind)}</td>
                <td>${participants}</td>
                <td>${formatTimestamp(rec.started_at ?? rec.created_at)}</td>
                <td>${formatDuration(rec.duration_seconds)}</td>
                <td>
                    ${playButton}
                    <button class="btn btn-secondary btn-sm" data-recording-transcript="${rec.id}">Transcript</button>
                </td>
            </tr>
        `;
    }).join('');

    container.innerHTML = `
        <table class="data-table">
            <thead>
                <tr>
                    <th>ID</th><th>Kind</th><th>Participants</th>
                    <th>Started</th><th>Duration</th><th>Actions</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

/**
 * Fetch a recording's audio as a blob URL.
 *
 * Same reason as voicemail: the endpoint authenticates with a bearer token, and an <audio src>
 * pointed at it makes the browser issue its own header-less request, which is rejected.
 * Fetching here carries the header and hands back a URL the browser will load. The caller must
 * revoke it, or the blob is held for the life of the page.
 */
async function fetchRecordingAudio(recordingId: number): Promise<string> {
    const API_BASE = getApiBaseUrl();
    const response = await fetch(`${API_BASE}/api/recordings/${recordingId}/audio`, {
        headers: getAuthHeaders()
    });

    if (!response.ok) throw new Error(reportFailure(response, 'play this recording'));
    return URL.createObjectURL(await response.blob());
}

export async function playRecording(recordingId: number): Promise<void> {
    const player = document.getElementById('recording-audio-player') as HTMLAudioElement | null;
    const section = document.getElementById('recording-player-section') as HTMLElement | null;
    if (!player) {
        showNotification('Audio player unavailable', 'error');
        return;
    }

    let objectUrl: string | null = null;
    try {
        objectUrl = await fetchRecordingAudio(recordingId);

        if (player.dataset.objectUrl) URL.revokeObjectURL(player.dataset.objectUrl);
        player.src = objectUrl;
        player.dataset.objectUrl = objectUrl;
        if (section) section.style.display = 'block';

        // Awaited so a playback failure surfaces here rather than as an unhandled rejection.
        await player.play();
    } catch (error: unknown) {
        if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
            delete player.dataset.objectUrl;
        }
        console.error('Error playing recording:', error);
        showNotification(error instanceof Error ? error.message : 'Failed to play recording', 'error');
    }
}

export async function downloadRecording(recordingId: number): Promise<void> {
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

export async function viewRecordingTranscript(recordingId: number): Promise<void> {
    const section = document.getElementById('recording-transcript-section') as HTMLElement | null;
    const body = document.getElementById('recording-transcript-body');
    if (!body) return;

    if (section) section.style.display = 'block';
    body.innerHTML = '<div class="info-box">Loading transcript...</div>';

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/recordings/${recordingId}/transcript`, {
            headers: getAuthHeaders()
        });

        if (response.status === 404) {
            body.innerHTML = '<div class="info-box">No transcript for this recording.</div>';
            return;
        }
        if (!response.ok) {
            body.innerHTML = `<div class="info-box">${escapeHtml(reportFailure(response, 'load this transcript'))}</div>`;
            return;
        }

        const data: TranscriptResponse = await response.json();
        renderTranscript(recordingId, data.transcripts ?? []);
    } catch (error: unknown) {
        console.error('Error loading transcript:', error);
        body.innerHTML = '<div class="info-box">Failed to load transcript</div>';
    }
}

function renderTranscript(recordingId: number, runs: TranscriptRun[]): void {
    const body = document.getElementById('recording-transcript-body');
    if (!body) return;

    if (runs.length === 0) {
        body.innerHTML = '<div class="info-box">No transcript for this recording.</div>';
        return;
    }

    // One row per transcription run, newest first -- re-transcribing with a better model
    // appends rather than overwriting, so more than one is normal and the older ones are
    // what somebody may already have read.
    body.innerHTML = `<h4>Recording ${recordingId}</h4>` + runs.map((run) => {
        // Whisper genuinely cannot report a confidence. Rendering a null as 0% reads as
        // "this transcript is worthless", which is a different claim from "not measured".
        const confidence = run.confidence === null || run.confidence === undefined
            ? 'Not reported'
            : `${Math.round(run.confidence * 100)}%`;

        const meta = [
            run.provider ? `Provider: ${escapeHtml(run.provider)}` : null,
            run.model ? `Model: ${escapeHtml(run.model)}` : null,
            run.language ? `Language: ${escapeHtml(run.language)}` : null,
            `Confidence: ${confidence}`,
            `Transcribed: ${formatTimestamp(run.created_at)}`
        ].filter(Boolean).join(' &middot; ');

        const text = (run.text ?? '').trim();
        return `
            <div class="transcription-box">
                <small style="color: #6b7280;">${meta}</small>
                <p>${text ? escapeHtml(text) : '<em>Transcript is empty.</em>'}</p>
            </div>
        `;
    }).join('');
}

export function closeRecordingPlayer(): void {
    const player = document.getElementById('recording-audio-player') as HTMLAudioElement | null;
    if (player) {
        player.pause();
        // Release the blob; without this it is held until the page reloads.
        if (player.dataset.objectUrl) {
            URL.revokeObjectURL(player.dataset.objectUrl);
            delete player.dataset.objectUrl;
        }
        player.src = '';
    }
    const section = document.getElementById('recording-player-section') as HTMLElement | null;
    if (section) section.style.display = 'none';
}

/**
 * Delegated click handling.
 *
 * Bound once to the list container rather than emitted as inline onclick attributes: the
 * handlers take a numeric id that would otherwise be interpolated into markup, and the CSP
 * work in this codebase is trying to retire 'unsafe-inline' rather than add to it.
 */
function bindRecordingActions(): void {
    const container = document.getElementById('call-recordings-list');
    if (!container || container.dataset.bound === 'true') return;
    container.dataset.bound = 'true';

    container.addEventListener('click', (event) => {
        const target = (event.target as HTMLElement | null)?.closest('button');
        if (!target) return;

        const play = target.dataset.recordingPlay;
        const download = target.dataset.recordingDownload;
        const transcript = target.dataset.recordingTranscript;

        if (play) void playRecording(Number(play));
        else if (download) void downloadRecording(Number(download));
        else if (transcript) void viewRecordingTranscript(Number(transcript));
    });
}

export function initCallRecordings(): void {
    bindRecordingActions();

    const filter = document.getElementById('recordings-kind-filter');
    if (filter && filter.dataset.bound !== 'true') {
        filter.dataset.bound = 'true';
        filter.addEventListener('change', () => void loadCallRecordings());
    }

    void loadCallRecordings();
}

window.initCallRecordings = initCallRecordings;
window.loadCallRecordings = loadCallRecordings;
window.closeRecordingPlayer = closeRecordingPlayer;
