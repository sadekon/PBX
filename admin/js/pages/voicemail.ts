/**
 * Voicemail page module.
 * Handles voicemail listing, playback, download, and management.
 */

import { getAuthHeaders, getApiBaseUrl } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
import { escapeHtml } from '../utils/html.ts';

interface VoicemailExtension {
    number: string;
    name: string;
}

interface VoicemailMessage {
    id: string;
    caller_id: string;
    timestamp: string;
    duration?: number;
    listened?: boolean;
    transcription?: string | null;
    transcription_confidence?: number | null;
    transcription_provider?: string | null;
}

interface VoicemailResponse {
    messages?: VoicemailMessage[];
}

/**
 * The messages currently on screen, by id.
 *
 * The transcript arrives with the listing, so playback does not re-fetch it -- the mailbox
 * response is already authorised for this extension and there is nothing further to ask for.
 */
const loadedMessages = new Map<string, VoicemailMessage>();

export async function loadVoicemailTab(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/extensions`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
        const extensions: VoicemailExtension[] = await response.json();

        const select = document.getElementById('vm-extension-select') as HTMLSelectElement | null;
        if (!select) return;
        select.innerHTML = '<option value="">Select Extension</option>';

        for (const ext of extensions) {
            const option = document.createElement('option');
            option.value = ext.number;
            option.textContent = `${ext.number} - ${ext.name}`;
            select.appendChild(option);
        }

        // Preselect the signed-in user's own mailbox -- the one they almost always want.
        // Stored at login; absent for a session that predates it, in which case the
        // placeholder stays selected and nothing is loaded until a choice is made.
        const ownExtension = localStorage.getItem('pbx_extension');
        if (ownExtension && extensions.some((ext) => ext.number === ownExtension)) {
            select.value = ownExtension;
            await loadVoicemailForExtension();
        }
    } catch (error: unknown) {
        console.error('Error loading voicemail tab:', error);
        showNotification('Failed to load extensions', 'error');
    }
}

export async function loadVoicemailForExtension(): Promise<void> {
    const extension = (document.getElementById('vm-extension-select') as HTMLSelectElement | null)?.value;
    if (!extension) {
        for (const id of ['voicemail-pin-section', 'voicemail-messages-section', 'voicemail-box-overview']) {
            const el = document.getElementById(id) as HTMLElement | null;
            if (el) el.style.display = 'none';
        }
        return;
    }

    for (const id of ['voicemail-pin-section', 'voicemail-messages-section', 'voicemail-box-overview']) {
        const el = document.getElementById(id) as HTMLElement | null;
        if (el) el.style.display = 'block';
    }

    const currentExt = document.getElementById('vm-current-extension') as HTMLElement | null;
    if (currentExt) currentExt.textContent = extension;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/voicemail/${extension}`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
        const data: VoicemailResponse = await response.json();

        updateVoicemailView(data.messages, extension);
    } catch (error: unknown) {
        console.error('Error loading voicemail:', error);
        showNotification('Failed to load voicemail messages', 'error');
    }
}

function updateVoicemailView(messages: VoicemailMessage[] | undefined, extension: string): void {
    const container = document.getElementById('voicemail-cards-view') as HTMLElement | null;
    if (!container) return;

    loadedMessages.clear();
    for (const msg of messages ?? []) loadedMessages.set(msg.id, msg);

    if (!messages || messages.length === 0) {
        container.innerHTML = '<div class="info-box">No voicemail messages</div>';
        return;
    }

    container.innerHTML = messages.map(msg => {
        const timestamp = new Date(msg.timestamp).toLocaleString();
        const duration = msg.duration ? `${msg.duration}s` : 'Unknown';
        const isUnread = !msg.listened;

        return `
            <div class="voicemail-card ${isUnread ? 'unread' : ''}">
                <div class="voicemail-card-header">
                    <div class="voicemail-from">${escapeHtml(msg.caller_id)}</div>
                    <span class="voicemail-status-badge ${isUnread ? 'unread' : 'read'}">
                        ${isUnread ? 'NEW' : 'READ'}
                    </span>
                </div>
                <div class="voicemail-card-body">
                    <div>Time: ${timestamp}</div>
                    <div>Duration: ${duration}</div>
                    ${renderTranscriptPreview(msg)}
                </div>
                <div class="voicemail-card-actions">
                    <button class="btn btn-primary btn-sm" onclick="playVoicemail('${extension}', '${msg.id}')">Play</button>
                    <button class="btn btn-secondary btn-sm" onclick="downloadVoicemail('${extension}', '${msg.id}')">Download</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteVoicemail('${extension}', '${msg.id}')">Delete</button>
                </div>
            </div>
        `;
    }).join('');
}

/** How much transcript a card shows before it needs the player to read the rest. */
const PREVIEW_CHARS = 180;

function renderTranscriptPreview(msg: VoicemailMessage): string {
    const text = (msg.transcription ?? '').trim();
    if (!text) return '';

    const truncated = text.length > PREVIEW_CHARS;
    const preview = truncated ? `${text.slice(0, PREVIEW_CHARS).trimEnd()}…` : text;
    return `<div class="voicemail-transcript-preview" style="margin-top: 8px; color: #4b5563;">
        <em>${escapeHtml(preview)}</em>
    </div>`;
}

/**
 * Show the transcript of the message being played, or hide the panel when there is none.
 *
 * Absent is the normal case, not a failure: transcription is optional, the engine may have
 * declined a message that was too short, and a message recorded before it was switched on
 * will never have one.
 */
function showTranscription(messageId: string): void {
    const display = document.getElementById('vm-transcription-display') as HTMLElement | null;
    const textEl = document.getElementById('vm-transcription-text');
    const confidenceEl = document.getElementById('vm-transcription-confidence');
    if (!display || !textEl) return;

    const msg = loadedMessages.get(messageId);
    const text = (msg?.transcription ?? '').trim();

    if (!text) {
        display.style.display = 'none';
        return;
    }

    textEl.textContent = text;

    if (confidenceEl) {
        // Whisper cannot report a confidence at all. Showing null as 0% claims the transcript
        // is worthless, which is a different statement from "not measured".
        const confidence = msg?.transcription_confidence;
        const provider = msg?.transcription_provider;
        const parts = [
            provider ? `Provider: ${provider}` : null,
            confidence === null || confidence === undefined
                ? 'Estimated accuracy: not reported'
                : `Estimated accuracy: ${Math.round(confidence * 100)}%`
        ].filter(Boolean);
        confidenceEl.textContent = parts.join(' · ');
    }

    display.style.display = 'block';
}

/**
 * Fetch a voicemail recording as a blob URL.
 *
 * The audio endpoint requires authentication, and the admin API authenticates with a bearer
 * token. Pointing an <audio src> or window.open() at it makes the browser issue its own
 * request, which carries no Authorization header and is rejected. Fetching it here means the
 * header travels with the request, and the caller gets a URL the browser will load.
 *
 * The caller must revoke the returned URL when finished, or the blob leaks for the lifetime
 * of the page.
 */
async function fetchVoicemailAudio(extension: string, messageId: string): Promise<string> {
    const API_BASE = getApiBaseUrl();
    const response = await fetch(`${API_BASE}/api/voicemail/${extension}/${messageId}/audio`, {
        headers: getAuthHeaders()
    });

    if (!response.ok) {
        throw new Error(
            response.status === 401
                ? 'Not authorized to play this voicemail - try signing in again'
                : `Could not fetch recording (HTTP ${response.status})`
        );
    }

    return URL.createObjectURL(await response.blob());
}

export async function playVoicemail(extension: string, messageId: string): Promise<void> {
    const player = document.getElementById('vm-audio-player') as HTMLAudioElement | null;
    if (!player) {
        showNotification('Audio player unavailable', 'error');
        return;
    }

    let objectUrl: string | null = null;
    try {
        objectUrl = await fetchVoicemailAudio(extension, messageId);

        // Release the previous recording's blob before replacing it.
        if (player.dataset.objectUrl) URL.revokeObjectURL(player.dataset.objectUrl);
        player.src = objectUrl;
        player.dataset.objectUrl = objectUrl;

        // closeVoicemailPlayer() hides this section and nothing used to bring it back, so
        // once a user closed the player every later Play was silent with no visible player.
        const playerSection = document.getElementById('voicemail-player-section') as HTMLElement | null;
        if (playerSection) playerSection.style.display = 'block';

        showTranscription(messageId);

        // Awaited so a playback failure is caught here rather than surfacing as an
        // unhandled rejection -- and so the message is only marked read once it has
        // actually started playing.
        await player.play();
        await markVoicemailRead(extension, messageId);
    } catch (error: unknown) {
        if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
            delete player.dataset.objectUrl;
        }
        console.error('Error playing voicemail:', error);
        showNotification(
            error instanceof Error ? error.message : 'Failed to play voicemail',
            'error'
        );
    }
}

export async function downloadVoicemail(extension: string, messageId: string): Promise<void> {
    let objectUrl: string | null = null;
    try {
        objectUrl = await fetchVoicemailAudio(extension, messageId);

        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = `voicemail_${extension}_${messageId}.wav`;
        document.body.appendChild(link);
        link.click();
        link.remove();
    } catch (error: unknown) {
        console.error('Error downloading voicemail:', error);
        showNotification(
            error instanceof Error ? error.message : 'Failed to download voicemail',
            'error'
        );
    } finally {
        // Safe to revoke immediately: the browser has already taken its own reference.
        if (objectUrl) URL.revokeObjectURL(objectUrl);
    }
}

export async function markVoicemailRead(extension: string, messageId: string): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        await fetch(`${API_BASE}/api/voicemail/${extension}/${messageId}/read`, {
            method: 'POST',
            headers: getAuthHeaders()
        });
    } catch (error: unknown) {
        console.error('Error marking voicemail read:', error);
    }
}

export async function deleteVoicemail(extension: string, messageId: string): Promise<void> {
    if (!confirm('Delete this voicemail message?')) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/voicemail/${extension}/${messageId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (response.ok) {
            showNotification('Voicemail deleted', 'success');
            loadVoicemailForExtension();
        } else {
            showNotification('Failed to delete voicemail', 'error');
        }
    } catch (error: unknown) {
        console.error('Error deleting voicemail:', error);
        showNotification('Failed to delete voicemail', 'error');
    }
}

export function closeVoicemailPlayer(): void {
    const player = document.getElementById('vm-audio-player') as HTMLAudioElement | null;
    if (player) {
        player.pause();
        // Release the recording's blob; without this it is held until the page reloads.
        if (player.dataset.objectUrl) {
            URL.revokeObjectURL(player.dataset.objectUrl);
            delete player.dataset.objectUrl;
        }
        player.src = '';
    }
    const playerSection = document.getElementById('voicemail-player-section') as HTMLElement | null;
    if (playerSection) playerSection.style.display = 'none';
}

export function toggleVoicemailView(): void {
    const cardsView = document.getElementById('voicemail-cards-view') as HTMLElement | null;
    const tableView = document.getElementById('voicemail-table-view') as HTMLElement | null;
    const toggleBtn = document.getElementById('toggle-voicemail-view-btn') as HTMLElement | null;

    if (cardsView && tableView) {
        const showingCards = cardsView.style.display !== 'none';
        cardsView.style.display = showingCards ? 'none' : 'block';
        tableView.style.display = showingCards ? 'block' : 'none';
        if (toggleBtn) toggleBtn.textContent = showingCards ? 'Switch to Card View' : 'Switch to Table View';
    }
}

// Backward compatibility
window.loadVoicemailTab = loadVoicemailTab;
window.loadVoicemailForExtension = loadVoicemailForExtension;
window.playVoicemail = playVoicemail;
window.downloadVoicemail = downloadVoicemail;
window.deleteVoicemail = deleteVoicemail;
window.markVoicemailRead = markVoicemailRead;
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- legacy backward compat
(window as any).closeVoicemailPlayer = closeVoicemailPlayer;
(window as any).toggleVoicemailView = toggleVoicemailView;
