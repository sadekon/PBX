/**
 * Call Queues (ACD) page module.
 * Queue CRUD, agent membership, agent login/pause state, and live status.
 */

import { getAuthHeaders, getApiBaseUrl } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
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

export async function loadQueuesData(): Promise<void> {
    await Promise.all([loadQueues(), loadQueueAgents()]);
}

export async function loadQueues(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/queues`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: QueuesResponse = await response.json();

        const tbody = document.getElementById('queues-table-body') as HTMLElement | null;
        if (!tbody) return;

        const queues = data.queues ?? [];
        if (queues.length === 0) {
            tbody.innerHTML = '<tr><td colspan="9">No call queues configured</td></tr>';
            return;
        }

        tbody.innerHTML = queues.map(q => `
            <tr>
                <td>${escapeHtml(q.queue_number)}</td>
                <td>${escapeHtml(q.name)}</td>
                <td>${escapeHtml(q.strategy)}</td>
                <td>${q.calls_waiting}${q.calls_waiting > 0 ? ` (${Math.round(q.longest_wait)}s)` : ''}</td>
                <td>${q.available_agents}/${q.total_agents}</td>
                <td>${escapeHtml(q.members.join(', ') || '—')}</td>
                <td>${escapeHtml(q.fallback_mailbox)}</td>
                <td>${q.enabled ? '✅' : '⛔'}</td>
                <td>
                    <button class="btn btn-secondary btn-sm" onclick="showEditQueueModal('${escapeHtml(q.queue_number)}')">Edit</button>
                    <button class="btn btn-success btn-sm" onclick="showAddQueueAgentModal('${escapeHtml(q.queue_number)}')">+ Agent</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteQueue('${escapeHtml(q.queue_number)}')">Delete</button>
                </td>
            </tr>
        `).join('');
    } catch (error: unknown) {
        console.error('Error loading queues:', error);
    }
}

export async function loadQueueAgents(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/queues/agents/state`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) return;
        const data: AgentStatesResponse = await response.json();

        const tbody = document.getElementById('queue-agents-table-body') as HTMLElement | null;
        if (!tbody) return;

        const agents = data.agents ?? [];
        if (agents.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7">No queue agents (add extensions to a queue first)</td></tr>';
            return;
        }

        tbody.innerHTML = agents.map(a => {
            const status = !a.logged_in
                ? 'Logged out'
                : a.paused
                    ? `Paused (${a.pause_reason ?? 'manual'})`
                    : 'Available';
            return `
            <tr>
                <td>${escapeHtml(a.extension)}</td>
                <td>${escapeHtml(a.queues.join(', ') || '—')}</td>
                <td>${escapeHtml(status)}</td>
                <td>${a.calls_taken}</td>
                <td>${a.consecutive_misses}</td>
                <td>${a.last_call_time ? escapeHtml(new Date(a.last_call_time).toLocaleString()) : '—'}</td>
                <td>
                    <button class="btn btn-secondary btn-sm" onclick="setQueueAgentState('${escapeHtml(a.extension)}', {logged_in: ${!a.logged_in}})">${a.logged_in ? 'Log out' : 'Log in'}</button>
                    <button class="btn btn-secondary btn-sm" onclick="setQueueAgentState('${escapeHtml(a.extension)}', {paused: ${!a.paused}})">${a.paused ? 'Unpause' : 'Pause'}</button>
                </td>
            </tr>
        `;
        }).join('');
    } catch (error: unknown) {
        console.error('Error loading queue agents:', error);
    }
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

export async function showAddQueueModal(): Promise<void> {
    const queueNumber = prompt('Queue Number (e.g., 8001):');
    if (!queueNumber) return;

    const name = prompt('Queue Name (e.g., "Sales"):');
    if (!name) return;

    const strategy = prompt(
        'Strategy (round_robin, least_recent, fewest_calls, random):',
        'round_robin'
    ) ?? 'round_robin';

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
            loadQueuesData();
        } else {
            showNotification(data.error ?? 'Failed to create queue', 'error');
        }
    } catch (error: unknown) {
        console.error('Error creating queue:', error);
        showNotification('Error creating queue', 'error');
    }
}

export async function showEditQueueModal(queueNumber: string): Promise<void> {
    const name = prompt('Queue Name (leave blank to keep):') ?? '';
    const strategy = prompt(
        'Strategy (round_robin, least_recent, fewest_calls, random; blank to keep):'
    ) ?? '';
    const ringTimeout = prompt('Ring timeout seconds (blank to keep):') ?? '';
    const maxWait = prompt('Max wait seconds before voicemail (blank to keep):') ?? '';
    const fallbackMailbox = prompt(
        'Fallback mailbox (blank to keep, "-" to reset to queue number):'
    ) ?? '';

    const payload: Record<string, unknown> = {};
    if (name) payload.name = name;
    if (strategy) payload.strategy = strategy;
    if (ringTimeout) payload.ring_timeout = parseInt(ringTimeout, 10);
    if (maxWait) payload.max_wait_time = parseInt(maxWait, 10);
    if (fallbackMailbox === '-') payload.fallback_mailbox = null;
    else if (fallbackMailbox) payload.fallback_mailbox = fallbackMailbox;

    if (Object.keys(payload).length === 0) return;

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

export async function showAddQueueAgentModal(queueNumber: string): Promise<void> {
    const extension = prompt(`Agent extension to add to queue ${queueNumber}:`);
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
window.showAddQueueModal = showAddQueueModal;
window.showEditQueueModal = showEditQueueModal;
window.deleteQueue = deleteQueue;
window.showAddQueueAgentModal = showAddQueueAgentModal;
window.removeQueueAgent = removeQueueAgent;
