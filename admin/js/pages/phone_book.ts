/**
 * Company directory page module.
 *
 * Read-only view of every extension on the system, available to all signed-in
 * users. The full list is fetched once and filtered in the browser, so typing
 * in the search box costs no round trips.
 */

import { fetchWithTimeout, getAuthHeaders, getApiBaseUrl } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
import { escapeHtml } from '../utils/html.ts';

interface DirectoryEntry {
    extension: string;
    name: string;
    email?: string | null;
    did_number?: string | null;
    registered: boolean;
    dnd_enabled: boolean;
    department?: string | null;
    mobile?: string | null;
    office_location?: string | null;
}

interface DirectoryResponse {
    entries?: DirectoryEntry[];
    count?: number;
}

const DIRECTORY_LOAD_TIMEOUT = 10000;
const COLUMN_COUNT = 6;

/** Full unfiltered directory, held so search never needs the network. */
let directoryEntries: DirectoryEntry[] = [];
let listenersAttached = false;

const el = (id: string): HTMLElement | null => document.getElementById(id);

/** Renders a value, or an italicised placeholder when the record lacks it. */
function orEmpty(value: string | null | undefined, placeholder = 'Not set'): string {
    return value ? escapeHtml(value) : `<span class="field-empty">${placeholder}</span>`;
}

function presenceCell(entry: DirectoryEntry): string {
    if (entry.dnd_enabled) {
        return '<span class="presence presence-dnd">Do Not Disturb</span>';
    }
    if (entry.registered) {
        return '<span class="presence presence-online">Online</span>';
    }
    return '<span class="presence">Offline</span>';
}

function matches(entry: DirectoryEntry, needle: string): boolean {
    return [
        entry.name,
        entry.extension,
        entry.email,
        entry.did_number,
        entry.department,
    ].some((field) => (field ?? '').toLowerCase().includes(needle));
}

function rowHtml(entry: DirectoryEntry): string {
    const extension = escapeHtml(entry.extension);
    const email = entry.email
        ? `<a href="mailto:${escapeHtml(entry.email)}">${escapeHtml(entry.email)}</a>`
        : `<span class="field-empty">Not set</span>`;

    return `
        <tr>
            <td><strong>${escapeHtml(entry.name)}</strong></td>
            <td>
                <button type="button" class="btn-link directory-dial"
                        data-extension="${extension}"
                        title="Call ${extension} from your phone">${extension}</button>
            </td>
            <td>${orEmpty(entry.did_number)}</td>
            <td>${email}</td>
            <td>${orEmpty(entry.department, '—')}</td>
            <td>${presenceCell(entry)}</td>
        </tr>
    `;
}

/** Applies the current search text to the cached list and repaints the table. */
function renderDirectory(): void {
    const tbody = el('phone-book-body');
    if (!tbody) return;

    const searchInput = el('directory-search') as HTMLInputElement | null;
    const needle = (searchInput?.value ?? '').trim().toLowerCase();
    const visible = needle
        ? directoryEntries.filter((entry) => matches(entry, needle))
        : directoryEntries;

    const countLabel = el('directory-count');
    if (countLabel) {
        if (directoryEntries.length === 0) {
            countLabel.textContent = '';
        } else if (needle) {
            countLabel.textContent = `${visible.length} of ${directoryEntries.length} shown`;
        } else {
            countLabel.textContent = `${directoryEntries.length} people`;
        }
    }

    if (visible.length === 0) {
        const message = needle
            ? `No one matches "${escapeHtml(needle)}"`
            : 'No extensions found';
        tbody.innerHTML = `<tr><td colspan="${COLUMN_COUNT}" class="loading">${message}</td></tr>`;
        return;
    }

    tbody.innerHTML = visible.map(rowHtml).join('');
}

function updateStats(): void {
    const online = directoryEntries.filter((entry) => entry.registered).length;
    const withDid = directoryEntries.filter((entry) => entry.did_number).length;

    const total = el('directory-total');
    if (total) total.textContent = String(directoryEntries.length);
    const onlineEl = el('directory-online');
    if (onlineEl) onlineEl.textContent = String(online);
    const didEl = el('directory-with-did');
    if (didEl) didEl.textContent = String(withDid);
}

export async function loadPhoneBook(): Promise<void> {
    const tbody = el('phone-book-body');
    if (!tbody) return;

    attachDirectoryListeners();
    tbody.innerHTML = `<tr><td colspan="${COLUMN_COUNT}" class="loading">Loading directory...</td></tr>`;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(
            `${API_BASE}/api/phone-book`,
            { headers: getAuthHeaders() },
            DIRECTORY_LOAD_TIMEOUT
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data: DirectoryResponse = await response.json();
        directoryEntries = data.entries ?? [];
        updateStats();
        renderDirectory();
    } catch (error: unknown) {
        console.error('Error loading directory:', error);
        const message = error instanceof Error ? error.message : String(error);
        const text = message === 'Request timed out'
            ? 'Request timed out. The system may still be starting.'
            : 'Error loading directory';
        tbody.innerHTML = `<tr><td colspan="${COLUMN_COUNT}" class="loading">${text}</td></tr>`;
    }
}

/** Rings the signed-in user's own phone, then connects it to the target. */
async function dialExtension(target: string): Promise<void> {
    const ownExtension = localStorage.getItem('pbx_extension');
    if (!ownExtension) {
        showNotification('Cannot place a call: no extension for the current user', 'error');
        return;
    }
    if (ownExtension === target) {
        showNotification('That is your own extension', 'info');
        return;
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(
            `${API_BASE}/api/framework/click-to-dial/call/${encodeURIComponent(ownExtension)}`,
            {
                method: 'POST',
                headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                body: JSON.stringify({ destination: target }),
            }
        );
        const result = await response.json();

        if (response.ok && !result.error) {
            showNotification(`Calling ${target} — your phone will ring first`, 'success');
        } else {
            showNotification(result.error || 'Failed to place the call', 'error');
        }
    } catch (error: unknown) {
        console.error('Error placing call:', error);
        showNotification('Failed to place the call', 'error');
    }
}

/**
 * Wires the toolbar and table once. Uses delegation and addEventListener
 * rather than inline onclick, so this page adds no new CSP unsafe-inline debt.
 */
function attachDirectoryListeners(): void {
    if (listenersAttached) return;

    const searchInput = el('directory-search') as HTMLInputElement | null;
    const tbody = el('phone-book-body');
    if (!searchInput || !tbody) return;

    // Filtering is local, so render on every keystroke rather than debouncing.
    searchInput.addEventListener('input', renderDirectory);
    searchInput.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key === 'Escape') {
            searchInput.value = '';
            renderDirectory();
        }
    });

    el('directory-refresh')?.addEventListener('click', () => {
        void loadPhoneBook();
    });

    tbody.addEventListener('click', (event: Event) => {
        const button = (event.target as HTMLElement).closest('.directory-dial');
        if (!button) return;
        const target = button.getAttribute('data-extension');
        if (target) void dialExtension(target);
    });

    listenersAttached = true;
}

// Backward compatibility
window.loadPhoneBook = loadPhoneBook;

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attachDirectoryListeners);
} else {
    attachDirectoryListeners();
}
