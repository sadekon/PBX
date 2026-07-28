/**
 * Call Queues (ACD) page module.
 * Queue CRUD, agent membership, agent login/pause state, and live status.
 */

import { getAuthHeaders, getApiBaseUrl } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
import { icon } from '../ui/icons.ts';
import { escapeHtml } from '../utils/html.ts';

interface QueueStatus {
    queue_number: string;
    name: string;
    strategy: string;
    enabled: boolean;
    ring_timeout: number;
    max_wait_time: number;
    max_queue_size: number;
    fallback_mailbox: string;
    auto_pause_misses: number;
    announcement_enabled: boolean;
    announcement_interval: number;
    announcement_text: string | null;
    announcement_file: string | null;
    announcement_position: boolean;
    members: string[];
    calls_waiting: number;
    longest_wait: number;
    total_agents: number;
    available_agents: number;
}

interface QueuesResponse {
    queues?: QueueStatus[];
}

interface AgentState {
    extension: string;
    logged_in: boolean;
    paused: boolean;
    pause_reason: string | null;
    consecutive_misses: number;
    calls_taken: number;
    last_call_time: string | null;
    queues: string[];
}

interface AgentStatesResponse {
    agents?: AgentState[];
}

interface ApiResponse {
    success?: boolean;
    message?: string;
    error?: string;
}

const QUEUE_STRATEGIES = ['round_robin', 'least_recent', 'fewest_calls', 'random'];

// Caches of the most recently loaded data, keyed for the combined render.
let queuesCache: QueueStatus[] = [];
let agentStatesCache = new Map<string, AgentState>();

// Queue numbers whose agent list is currently collapsed. Persists across
// re-renders so an action (pause, remove, …) doesn't re-expand a card.
const collapsedQueues = new Set<string>();

export async function loadQueuesData(): Promise<void> {
    await Promise.all([fetchQueues(), fetchAgentStates()]);
    renderQueueCards();
}

// Kept for backward compatibility / targeted refresh: each fetches its own
// slice then re-renders the full set of cards.
export async function loadQueues(): Promise<void> {
    await fetchQueues();
    renderQueueCards();
}

export async function loadQueueAgents(): Promise<void> {
    await fetchAgentStates();
    renderQueueCards();
}

async function fetchQueues(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/queues`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: QueuesResponse = await response.json();
        queuesCache = data.queues ?? [];
    } catch (error: unknown) {
        console.error('Error loading queues:', error);
    }
}

async function fetchAgentStates(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/queues/agents/state`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) return;
        const data: AgentStatesResponse = await response.json();
        agentStatesCache = new Map((data.agents ?? []).map(a => [a.extension, a]));
    } catch (error: unknown) {
        console.error('Error loading queue agents:', error);
    }
}

function renderStatusPill(state: AgentState | undefined): string {
    const loggedIn = state?.logged_in ?? false;
    const paused = state?.paused ?? false;
    let cls: string;
    let label: string;
    let title: string;
    if (!loggedIn) {
        cls = 'off';
        label = 'Logged out';
        title = 'Logged out';
    } else if (paused) {
        cls = 'warn';
        label = 'Paused';
        title = state?.pause_reason ? `Paused (${state.pause_reason})` : 'Paused';
    } else {
        cls = 'ok';
        label = 'Available';
        title = 'Available';
    }
    return `<span class="status-pill ${cls}" title="${escapeHtml(title)}"><span class="status-dot"></span>${label}</span>`;
}

function renderAgentRow(queueNumber: string, extension: string): string {
    const state = agentStatesCache.get(extension);
    const loggedIn = state?.logged_in ?? false;
    const paused = state?.paused ?? false;
    const callsTaken = state?.calls_taken ?? 0;
    const misses = state?.consecutive_misses ?? 0;
    const lastCall = state?.last_call_time
        ? escapeHtml(new Date(state.last_call_time).toLocaleString())
        : '—';
    const ext = escapeHtml(extension);
    const qn = escapeHtml(queueNumber);

    // Presence toggle: green log-in arrow when logged out, grey log-out when in.
    const loginTitle = loggedIn ? 'Log out' : 'Log in';
    const loginClass = loggedIn ? 'icon-btn' : 'icon-btn icon-btn-on';
    const loginBtn =
        `<button type="button" class="${loginClass}" title="${loginTitle}" aria-label="${loginTitle}" ` +
        `onclick="setQueueAgentState('${ext}', {logged_in: ${!loggedIn}})">${icon(loggedIn ? 'log-out' : 'log-in')}</button>`;

    // Pause is only meaningful while logged in; keep the slot but disable it
    // when logged out so the Remove button stays in a fixed column.
    const pauseTitle = paused ? 'Resume' : 'Pause';
    const pauseBtn = loggedIn
        ? `<button type="button" class="icon-btn" title="${pauseTitle}" aria-label="${pauseTitle}" ` +
          `onclick="setQueueAgentState('${ext}', {paused: ${!paused}})">${icon(paused ? 'play' : 'pause')}</button>`
        : `<button type="button" class="icon-btn" title="Log in to pause" aria-label="Pause (log in first)" disabled>${icon('pause')}</button>`;

    const removeBtn =
        `<button type="button" class="icon-btn icon-btn-danger" title="Remove from queue" aria-label="Remove from queue" ` +
        `onclick="removeQueueAgent('${qn}', '${ext}')">${icon('trash')}</button>`;

    return `
        <tr class="queue-agent-row">
            <td class="queue-agent-ext">${ext}</td>
            <td>${renderStatusPill(state)}</td>
            <td>${callsTaken}</td>
            <td>${misses}</td>
            <td>${lastCall}</td>
            <td>
                <div class="agent-actions">${loginBtn}${pauseBtn}${removeBtn}</div>
            </td>
        </tr>
    `;
}

function renderQueueCard(q: QueueStatus): string {
    const qn = escapeHtml(q.queue_number);
    const collapsed = collapsedQueues.has(q.queue_number);
    const waiting = `${q.calls_waiting}${q.calls_waiting > 0 ? ` (${Math.round(q.longest_wait)}s)` : ''}`;

    // Single-row header: identity + enabled state + the queue's own
    // attributes as labeled stats, with queue-level actions on the right.
    const head = `
        <div class="queue-card-head">
            <span class="qch-title" role="button" aria-expanded="${!collapsed}" data-queue-toggle="${qn}" onclick="toggleQueueCollapse('${qn}')">
                <span class="queue-chevron${collapsed ? '' : ' open'}" aria-hidden="true">▶</span>
                <strong>${qn}</strong> — ${escapeHtml(q.name)}
            </span>
            <span class="en-pill ${q.enabled ? 'on' : 'off'}"><span class="en-dot"></span>${q.enabled ? 'Enabled' : 'Disabled'}</span>
            <span class="qch-stats">
                <span class="qch-stat"><span class="k">Strategy</span><span class="v">${escapeHtml(q.strategy)}</span></span>
                <span class="qch-stat"><span class="k">Waiting</span><span class="v">${waiting}</span></span>
                <span class="qch-stat"><span class="k">Online</span><span class="v">${q.available_agents} / ${q.total_agents}</span></span>
                <span class="qch-stat"><span class="k">Overflow</span><span class="v">${escapeHtml(q.fallback_mailbox)}</span></span>
            </span>
            <span class="qch-actions">
                <button type="button" class="qbtn" onclick="showEditQueueModal('${qn}')">${icon('pencil')}Edit</button>
                <button type="button" class="qbtn qbtn-danger" onclick="deleteQueue('${qn}')">${icon('trash')}Delete</button>
            </span>
        </div>
    `;

    // Agents get their own table with headers that actually describe them,
    // rather than sharing a header row with the queue's own attributes.
    const agentTable = q.members.length > 0
        ? `<table class="agent-subtable">
               <thead>
                   <tr>
                       <th>Extension</th><th>Status</th><th>Calls Taken</th>
                       <th>Missed (consec.)</th><th>Last Call</th><th>Actions</th>
                   </tr>
               </thead>
               <tbody>${q.members.map(ext => renderAgentRow(q.queue_number, ext)).join('')}</tbody>
           </table>`
        : `<div class="queue-empty-hint">No agents assigned yet.</div>`;

    // "Add agent" lives at the end of the queue's agent list, doubling as the
    // empty-state action, rather than as a button in the card header.
    const addBtn = `<button type="button" class="add-agent-btn" onclick="showAddQueueAgentModal('${qn}')">${icon('user-plus')}Add agent</button>`;

    return `
        <div class="queue-card">
            ${head}
            <div class="queue-card-body${collapsed ? ' collapsed' : ''}" data-queue-body="${qn}">
                ${agentTable}
                ${addBtn}
            </div>
        </div>
    `;
}

export function toggleQueueCollapse(queueNumber: string): void {
    if (collapsedQueues.has(queueNumber)) {
        collapsedQueues.delete(queueNumber);
    } else {
        collapsedQueues.add(queueNumber);
    }
    const collapsed = collapsedQueues.has(queueNumber);
    const key = typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(queueNumber) : queueNumber;
    document.querySelector(`[data-queue-body="${key}"]`)?.classList.toggle('collapsed', collapsed);
    const toggle = document.querySelector(`[data-queue-toggle="${key}"]`);
    if (toggle) {
        toggle.setAttribute('aria-expanded', String(!collapsed));
        toggle.querySelector('.queue-chevron')?.classList.toggle('open', !collapsed);
    }
}

function renderQueueCards(): void {
    const container = document.getElementById('queues-cards') as HTMLElement | null;
    if (!container) return;

    if (queuesCache.length === 0) {
        container.innerHTML = `<div class="queues-empty">No call queues configured</div>`;
        return;
    }

    container.innerHTML = queuesCache.map(renderQueueCard).join('');
}

export async function setQueueAgentState(
    extension: string,
    state: { logged_in?: boolean; paused?: boolean }
): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/queues/agents/${extension}/state`, {
            method: 'PUT',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify(state)
        });
        const data: ApiResponse = await response.json();
        if (data.success) {
            showNotification(`Agent ${extension} updated`, 'success');
            loadQueuesData();
        } else {
            showNotification(data.error ?? 'Failed to update agent', 'error');
        }
    } catch (error: unknown) {
        console.error('Error updating agent state:', error);
        showNotification('Error updating agent state', 'error');
    }
}

function strategyOptions(selected: string): string {
    return QUEUE_STRATEGIES.map(
        s => `<option value="${s}"${s === selected ? ' selected' : ''}>${s}</option>`
    ).join('');
}

export function closeQueueModal(): void {
    document.getElementById('queue-form-modal')?.remove();
}

export function showAddQueueModal(): void {
    closeQueueModal();
    const modal = `
        <div id="queue-form-modal" class="modal" style="display: block;">
            <div class="modal-content">
                <div class="modal-header">
                    <h3>➕ Add Call Queue</h3>
                    <span class="close" onclick="closeQueueModal()">&times;</span>
                </div>
                <form id="queue-form">
                    <div class="form-group">
                        <label for="queue-number">Queue Number:</label>
                        <input type="text" id="queue-number" required placeholder="8001">
                        <small>Extension callers dial to reach this queue</small>
                    </div>
                    <div class="form-group">
                        <label for="queue-name">Queue Name:</label>
                        <input type="text" id="queue-name" required placeholder="Sales">
                        <small>Descriptive name for this queue</small>
                    </div>
                    <div class="form-group">
                        <label for="queue-strategy">Ring Strategy:</label>
                        <select id="queue-strategy">${strategyOptions('round_robin')}</select>
                        <small>How calls are distributed to agents</small>
                    </div>
                    <div class="modal-actions">
                        <button type="button" class="btn btn-secondary" onclick="closeQueueModal()">Cancel</button>
                        <button type="submit" class="btn btn-success">Add Queue</button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', modal);
    const form = document.getElementById('queue-form') as HTMLFormElement;
    form.onsubmit = (e: Event) => {
        e.preventDefault();
        void submitAddQueue();
    };
}

async function submitAddQueue(): Promise<void> {
    const queueNumber = (document.getElementById('queue-number') as HTMLInputElement).value.trim();
    const name = (document.getElementById('queue-name') as HTMLInputElement).value.trim();
    const strategy = (document.getElementById('queue-strategy') as HTMLSelectElement).value;
    if (!queueNumber || !name) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/queues`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ queue_number: queueNumber, name: name, strategy: strategy })
        });
        const data: ApiResponse = await response.json();
        if (data.success) {
            showNotification(`Queue ${queueNumber} created`, 'success');
            closeQueueModal();
            loadQueuesData();
        } else {
            showNotification(data.error ?? 'Failed to create queue', 'error');
        }
    } catch (error: unknown) {
        console.error('Error creating queue:', error);
        showNotification('Error creating queue', 'error');
    }
}

export function showEditQueueModal(queueNumber: string): void {
    closeQueueModal();
    const queue = queuesCache.find(q => q.queue_number === queueNumber);
    const modal = `
        <div id="queue-form-modal" class="modal" style="display: block;">
            <div class="modal-content">
                <div class="modal-header">
                    <h3>✏️ Edit Queue ${escapeHtml(queueNumber)}</h3>
                    <span class="close" onclick="closeQueueModal()">&times;</span>
                </div>
                <form id="queue-form">
                    <div class="form-group">
                        <label for="queue-name">Queue Name:</label>
                        <input type="text" id="queue-name" value="${escapeHtml(queue?.name ?? '')}">
                    </div>
                    <div class="form-group">
                        <label for="queue-strategy">Ring Strategy:</label>
                        <select id="queue-strategy">${strategyOptions(queue?.strategy ?? 'round_robin')}</select>
                    </div>
                    <div class="form-group">
                        <label for="queue-ring-timeout">Ring Timeout (seconds):</label>
                        <input type="number" id="queue-ring-timeout" min="1" value="${queue?.ring_timeout ?? ''}">
                    </div>
                    <div class="form-group">
                        <label for="queue-max-wait">Max Wait Before Voicemail (seconds):</label>
                        <input type="number" id="queue-max-wait" min="1" value="${queue?.max_wait_time ?? ''}">
                    </div>
                    <div class="form-group">
                        <label for="queue-fallback">Fallback Mailbox:</label>
                        <input type="text" id="queue-fallback" value="${escapeHtml(queue?.fallback_mailbox ?? '')}" placeholder="Blank to keep, - to reset to queue number">
                        <small>Where calls go after max wait. Enter "-" to reset to the queue number.</small>
                    </div>
                    <div class="form-group">
                        <label for="queue-announcement-enabled">
                            <input type="checkbox" id="queue-announcement-enabled" ${queue?.announcement_enabled ? 'checked' : ''}>
                            Hold Announcements
                        </label>
                        <small>Periodically interrupt hold music with a message.</small>
                    </div>
                    <div class="form-group">
                        <label for="queue-announcement-interval">Announcement Interval (seconds):</label>
                        <input type="number" id="queue-announcement-interval" min="10" value="${queue?.announcement_interval ?? ''}">
                    </div>
                    <div class="form-group">
                        <label for="queue-announcement-text">Custom Message:</label>
                        <input type="text" id="queue-announcement-text" value="${escapeHtml(queue?.announcement_text ?? '')}" placeholder="Blank for the default message">
                        <small>Spoken via text-to-speech. Ignored if a pre-recorded file is set below.</small>
                    </div>
                    <div class="form-group">
                        <label for="queue-announcement-file">Pre-recorded Announcement File:</label>
                        <input type="text" id="queue-announcement-file" value="${escapeHtml(queue?.announcement_file ?? '')}" placeholder="e.g. sales.wav">
                        <small>Filename under moh/announcements/. Takes priority over the custom message.</small>
                    </div>
                    <div class="form-group">
                        <label for="queue-announcement-position">
                            <input type="checkbox" id="queue-announcement-position" ${queue?.announcement_position ? 'checked' : ''}>
                            Announce Caller Position
                        </label>
                        <small>Appends "you are caller number N" to the message (ignored for a pre-recorded file).</small>
                    </div>
                    <div class="modal-actions">
                        <button type="button" class="btn btn-secondary" onclick="closeQueueModal()">Cancel</button>
                        <button type="submit" class="btn btn-success">Save Changes</button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', modal);
    const form = document.getElementById('queue-form') as HTMLFormElement;
    form.onsubmit = (e: Event) => {
        e.preventDefault();
        void submitEditQueue(queueNumber);
    };
}

async function submitEditQueue(queueNumber: string): Promise<void> {
    const name = (document.getElementById('queue-name') as HTMLInputElement).value.trim();
    const strategy = (document.getElementById('queue-strategy') as HTMLSelectElement).value;
    const ringTimeout = (document.getElementById('queue-ring-timeout') as HTMLInputElement).value.trim();
    const maxWait = (document.getElementById('queue-max-wait') as HTMLInputElement).value.trim();
    const fallbackMailbox = (document.getElementById('queue-fallback') as HTMLInputElement).value.trim();
    const announcementEnabled = (document.getElementById('queue-announcement-enabled') as HTMLInputElement).checked;
    const announcementInterval = (document.getElementById('queue-announcement-interval') as HTMLInputElement).value.trim();
    const announcementText = (document.getElementById('queue-announcement-text') as HTMLInputElement).value.trim();
    const announcementFile = (document.getElementById('queue-announcement-file') as HTMLInputElement).value.trim();
    const announcementPosition = (document.getElementById('queue-announcement-position') as HTMLInputElement).checked;

    const payload: Record<string, unknown> = {};
    if (name) payload.name = name;
    if (strategy) payload.strategy = strategy;
    if (ringTimeout) payload.ring_timeout = parseInt(ringTimeout, 10);
    if (maxWait) payload.max_wait_time = parseInt(maxWait, 10);
    if (fallbackMailbox === '-') payload.fallback_mailbox = null;
    else if (fallbackMailbox) payload.fallback_mailbox = fallbackMailbox;
    payload.announcement_enabled = announcementEnabled;
    payload.announcement_position = announcementPosition;
    if (announcementInterval) payload.announcement_interval = parseInt(announcementInterval, 10);
    payload.announcement_text = announcementText || null;
    payload.announcement_file = announcementFile || null;

    if (Object.keys(payload).length === 0) {
        closeQueueModal();
        return;
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/queues/${queueNumber}`, {
            method: 'PUT',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data: ApiResponse = await response.json();
        if (data.success) {
            showNotification(`Queue ${queueNumber} updated`, 'success');
            closeQueueModal();
            loadQueuesData();
        } else {
            showNotification(data.error ?? 'Failed to update queue', 'error');
        }
    } catch (error: unknown) {
        console.error('Error updating queue:', error);
        showNotification('Error updating queue', 'error');
    }
}

export async function deleteQueue(queueNumber: string): Promise<void> {
    if (!confirm(`Delete queue ${queueNumber}?`)) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/queues/${queueNumber}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });
        const data: ApiResponse = await response.json();
        if (data.success) {
            showNotification(`Queue ${queueNumber} deleted`, 'success');
            loadQueuesData();
        } else {
            showNotification(data.error ?? 'Failed to delete queue', 'error');
        }
    } catch (error: unknown) {
        console.error('Error deleting queue:', error);
        showNotification('Error deleting queue', 'error');
    }
}

export function closeQueueAgentModal(): void {
    document.getElementById('queue-agent-modal')?.remove();
}

export function showAddQueueAgentModal(queueNumber: string): void {
    closeQueueAgentModal();
    const modal = `
        <div id="queue-agent-modal" class="modal" style="display: block;">
            <div class="modal-content">
                <div class="modal-header">
                    <h3>➕ Add Agent to Queue ${escapeHtml(queueNumber)}</h3>
                    <span class="close" onclick="closeQueueAgentModal()">&times;</span>
                </div>
                <form id="queue-agent-form">
                    <div class="form-group">
                        <label for="queue-agent-extension">Agent Extension:</label>
                        <input type="text" id="queue-agent-extension" required placeholder="1001">
                        <small>Extension of the agent to add to this queue</small>
                    </div>
                    <div class="modal-actions">
                        <button type="button" class="btn btn-secondary" onclick="closeQueueAgentModal()">Cancel</button>
                        <button type="submit" class="btn btn-success">Add Agent</button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', modal);
    const form = document.getElementById('queue-agent-form') as HTMLFormElement;
    form.onsubmit = (e: Event) => {
        e.preventDefault();
        void submitAddQueueAgent(queueNumber);
    };
}

async function submitAddQueueAgent(queueNumber: string): Promise<void> {
    const extension = (document.getElementById('queue-agent-extension') as HTMLInputElement).value.trim();
    if (!extension) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/queues/${queueNumber}/agents`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ extension: extension })
        });
        const data: ApiResponse = await response.json();
        if (data.success) {
            showNotification(`Agent ${extension} added to ${queueNumber}`, 'success');
            closeQueueAgentModal();
            loadQueuesData();
        } else {
            showNotification(data.error ?? 'Failed to add agent', 'error');
        }
    } catch (error: unknown) {
        console.error('Error adding agent:', error);
        showNotification('Error adding agent', 'error');
    }
}

export async function removeQueueAgent(queueNumber: string, extension: string): Promise<void> {
    if (!confirm(`Remove agent ${extension} from queue ${queueNumber}?`)) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/queues/${queueNumber}/agents/${extension}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });
        const data: ApiResponse = await response.json();
        if (data.success) {
            showNotification(`Agent ${extension} removed from ${queueNumber}`, 'success');
            loadQueuesData();
        } else {
            showNotification(data.error ?? 'Failed to remove agent', 'error');
        }
    } catch (error: unknown) {
        console.error('Error removing agent:', error);
        showNotification('Error removing agent', 'error');
    }
}

// Backward compatibility
window.loadQueuesData = loadQueuesData;
window.loadQueues = loadQueues;
window.loadQueueAgents = loadQueueAgents;
window.setQueueAgentState = setQueueAgentState;
window.toggleQueueCollapse = toggleQueueCollapse;
window.showAddQueueModal = showAddQueueModal;
window.showEditQueueModal = showEditQueueModal;
window.closeQueueModal = closeQueueModal;
window.deleteQueue = deleteQueue;
window.showAddQueueAgentModal = showAddQueueAgentModal;
window.closeQueueAgentModal = closeQueueAgentModal;
window.removeQueueAgent = removeQueueAgent;
