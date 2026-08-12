/**
 * Company directory page module.
 *
 * Read-only view of every extension on the system, available to all signed-in
 * users. The full list is fetched once and filtered in the browser, so typing
 * in the search box costs no round trips.
 *
 * Presentation follows the shared vocabulary in patterns.css (card-stack,
 * pill, meta-stats, filter-bar), the same one the call recordings page uses.
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

/**
 * Group by department only once most of the directory has one.
 *
 * Department is optional enrichment, so below this share the page would be one
 * long "No department" heading with a short real group above it — worse than
 * not grouping. Above it, the headings carry the structure. The threshold is
 * deliberately high: partial grouping reads as missing data, not as a category.
 */
const GROUPING_THRESHOLD = 0.6;

/** Full unfiltered directory, held so search never needs the network. */
let directoryEntries: DirectoryEntry[] = [];
let listenersAttached = false;

/** Extensions whose card body is open. Survives re-renders from filtering. */
const expanded = new Set<string>();

const el = (id: string): HTMLElement | null => document.getElementById(id);

/** Renders a value, or an italicised placeholder when the record lacks it. */
function orEmpty(value: string | null | undefined, placeholder = 'Not set'): string {
    return value ? escapeHtml(value) : `<span class="field-empty">${placeholder}</span>`;
}

interface NameParts {
    first: string;
    last: string;
}

/**
 * Splits a display name into forename and surname.
 *
 * Handles both orders Active Directory produces: "Maya Rodriguez" and the comma
 * form "Rodriguez, Maya". Everything before the last space is the forename, so a
 * particle stays with it and "Jean-Luc de Vries" files under V — which is also
 * how the Dutch convention sorts it. A single word is its own surname, covering
 * mononyms and service accounts.
 */
function splitName(name: string): NameParts {
    const trimmed = name.trim();

    const comma = trimmed.indexOf(',');
    if (comma !== -1) {
        return {
            last: trimmed.slice(0, comma).trim(),
            first: trimmed.slice(comma + 1).trim(),
        };
    }

    const words = trimmed.split(/\s+/).filter(Boolean);
    if (words.length <= 1) {
        return { first: '', last: words[0] ?? '' };
    }
    return {
        first: words.slice(0, -1).join(' '),
        last: words[words.length - 1]!,
    };
}

/**
 * Up to two initials for the avatar.
 *
 * "Maya Rodriguez" gives MR, "Jean-Luc de Vries" gives JV rather than JD, and
 * the comma form "Rodriguez, Maya" gives MR rather than RM.
 */
function initials(name: string): string {
    const { first, last } = splitName(name);
    const combined = first.charAt(0) + last.charAt(0);
    return combined ? combined.toUpperCase() : '?';
}

/**
 * Presence as a dot on the avatar rather than a pill on every row.
 *
 * Offline deliberately renders no dot at all, rather than a grey one. The three
 * states then differ by something other than hue — no dot, dot, dot plus a
 * "Do not disturb" pill — so they stay distinguishable to a colourblind reader.
 * The label is repeated for screen readers, which see none of it.
 */
function avatarHtml(entry: DirectoryEntry): string {
    let dot = '';
    let label = 'Offline';
    if (entry.dnd_enabled) {
        dot = '<span class="avatar-dot avatar-dot-warn"></span>';
        label = 'Do not disturb';
    } else if (entry.registered) {
        dot = '<span class="avatar-dot avatar-dot-ok"></span>';
        label = 'Online';
    }
    return `<span class="avatar" title="${label}" aria-hidden="true">${escapeHtml(initials(entry.name))}${dot}</span><span class="sr-only">${label}</span>`;
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

function currentNeedle(): string {
    const searchInput = el('directory-search') as HTMLInputElement | null;
    return (searchInput?.value ?? '').trim().toLowerCase();
}

function onlineOnly(): boolean {
    return (el('directory-online-only') as HTMLInputElement | null)?.checked ?? false;
}

/**
 * The expanded half of a card: the fields that have no room in the header.
 *
 * mobile, office_location and department are returned by the API and appear
 * nowhere else in the interface, so this is the only place they surface.
 *
 * Only populated fields are listed. All three are optional enrichment that is
 * frequently absent for everyone at once, and a row of "Not set / Not set / —"
 * on every card is the same noise the pill rules above warn against: it costs a
 * line of reading to learn nothing. When a record carries none of them the card
 * says so once, in one short line.
 */
function cardBodyHtml(entry: DirectoryEntry): string {
    const fields: string[] = [];

    if (entry.email) {
        const address = escapeHtml(entry.email);
        fields.push(`<span class="meta-stat"><span class="k">Email</span><span class="v"><a href="mailto:${address}">${address}</a></span></span>`);
    }
    if (entry.mobile) {
        fields.push(`<span class="meta-stat"><span class="k">Mobile</span><span class="v">${escapeHtml(entry.mobile)}</span></span>`);
    }
    if (entry.office_location) {
        fields.push(`<span class="meta-stat"><span class="k">Office</span><span class="v">${escapeHtml(entry.office_location)}</span></span>`);
    }
    if (entry.department) {
        fields.push(`<span class="meta-stat"><span class="k">Department</span><span class="v">${escapeHtml(entry.department)}</span></span>`);
    }

    if (fields.length === 0) {
        return '<div class="muted-note" style="padding: 4px 16px 10px;">No email, mobile, office or department on record.</div>';
    }

    return `
        <div style="padding: 8px 16px;">
            <span class="meta-stats" style="gap: 28px;">${fields.join('')}</span>
        </div>
    `;
}

function cardHtml(entry: DirectoryEntry): string {
    const ext = escapeHtml(entry.extension);
    const open = expanded.has(entry.extension);

    // Only a genuine exception earns a pill. Online is the norm here, and the
    // avatar dot already carries it.
    const pill = entry.dnd_enabled
        ? '<span class="pill pill-warn">Do not disturb</span>'
        : '';

    const emailAction = entry.email
        ? `<a class="btn-ghost" href="mailto:${escapeHtml(entry.email)}">Email</a>`
        : '';

    return `
        <div class="card-shell">
            <div class="card-head">
                <span class="card-title" role="button" tabindex="0"
                      aria-expanded="${open}" data-dir-toggle="${ext}">
                    <span class="card-chevron${open ? ' open' : ''}" aria-hidden="true">&#9654;</span>
                    ${avatarHtml(entry)}
                    <strong>${escapeHtml(entry.name)}</strong>
                </span>
                ${pill}
                <span class="meta-stats">
                    <span class="meta-stat"><span class="k">Ext</span><span class="v">${ext}</span></span>
                    <span class="meta-stat"><span class="k">Direct</span><span class="v">${orEmpty(entry.did_number)}</span></span>
                </span>
                <span class="card-actions">
                    <button type="button" class="btn-ghost" data-dir-dial="${ext}"
                            title="Call ${ext} from your phone">Call</button>
                    ${emailAction}
                </span>
            </div>
            <div class="card-body${open ? '' : ' collapsed'}" data-dir-body="${ext}">
                ${open ? cardBodyHtml(entry) : ''}
            </div>
        </div>
    `;
}

/** True when enough of the directory has a department for headings to help. */
function shouldGroup(entries: DirectoryEntry[]): boolean {
    if (entries.length === 0) return false;
    const withDept = entries.filter((entry) => entry.department).length;
    return withDept / entries.length >= GROUPING_THRESHOLD;
}

type SortKey = 'name' | 'extension' | 'status';

function currentSort(): SortKey {
    const value = (el('directory-sort') as HTMLSelectElement | null)?.value;
    return value === 'extension' || value === 'status' ? value : 'name';
}

/** Online first, then do-not-disturb, then offline — most reachable first. */
function presenceRank(entry: DirectoryEntry): number {
    if (entry.dnd_enabled) return 1;
    return entry.registered ? 0 : 2;
}

/**
 * Surname first, forename as the tiebreak — the order a printed directory uses,
 * so the two Rodriguezes sit together rather than being separated by whoever
 * else happens to share their forename's initial.
 */
function bySurname(a: DirectoryEntry, b: DirectoryEntry): number {
    const left = splitName(a.name);
    const right = splitName(b.name);
    return left.last.localeCompare(right.last) || left.first.localeCompare(right.first);
}

/**
 * Orders one run of people by the current dropdown.
 *
 * Every key falls back to name, so the list has a stable order rather than
 * leaving everyone who shares a status in whatever sequence the API returned.
 * When grouping is active this runs per department, not across the whole list.
 */
function sorted(entries: DirectoryEntry[]): DirectoryEntry[] {
    const key = currentSort();

    return [...entries].sort((a, b) => {
        if (key === 'extension') {
            // numeric so 999 comes before 1000, which a plain string sort reverses.
            const order = a.extension.localeCompare(b.extension, undefined, { numeric: true });
            return order || bySurname(a, b);
        }
        if (key === 'status') {
            return presenceRank(a) - presenceRank(b) || bySurname(a, b);
        }
        return bySurname(a, b);
    });
}

/** Cards, grouped under department headings when the data supports it. */
function listHtml(visible: DirectoryEntry[]): string {
    if (!shouldGroup(visible)) {
        return sorted(visible).map(cardHtml).join('');
    }

    const byDepartment = new Map<string, DirectoryEntry[]>();
    for (const entry of visible) {
        const key = entry.department || 'Unassigned';
        const bucket = byDepartment.get(key);
        if (bucket) {
            bucket.push(entry);
        } else {
            byDepartment.set(key, [entry]);
        }
    }

    // Alphabetical, but Unassigned last however it sorts — it is a leftover
    // bucket rather than a department, so it does not belong among them.
    const names = [...byDepartment.keys()].sort((a, b) => {
        if (a === 'Unassigned') return 1;
        if (b === 'Unassigned') return -1;
        return a.localeCompare(b);
    });

    return names
        .map((name) => {
            const cards = sorted(byDepartment.get(name) ?? []).map(cardHtml).join('');
            return `<div class="group-label">${escapeHtml(name)}</div>${cards}`;
        })
        .join('');
}

/** Applies the current filters to the cached list and repaints. */
function renderDirectory(): void {
    const container = el('directory-list');
    if (!container) return;

    const needle = currentNeedle();
    const online = onlineOnly();

    const visible = directoryEntries.filter((entry) => {
        if (needle && !matches(entry, needle)) return false;
        return !(online && !entry.registered);
    });

    const filtering = Boolean(needle) || online;

    const clearButton = el('directory-clear-filter');
    if (clearButton) clearButton.hidden = !filtering;

    const countLabel = el('directory-count');
    if (countLabel) {
        if (directoryEntries.length === 0) {
            countLabel.textContent = '';
        } else if (filtering) {
            countLabel.textContent = `${visible.length} of ${directoryEntries.length} shown`;
        } else {
            const plural = directoryEntries.length === 1 ? 'person' : 'people';
            countLabel.textContent = `${directoryEntries.length} ${plural}`;
        }
    }

    if (visible.length === 0) {
        const message = filtering ? 'No one matches those filters' : 'No extensions found';
        container.innerHTML = `<div class="list-empty">${message}</div>`;
        return;
    }

    container.innerHTML = `<div class="card-stack">${listHtml(visible)}</div>`;
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
    const container = el('directory-list');
    if (!container) return;

    attachDirectoryListeners();
    container.innerHTML = '<div class="list-loading">Loading directory...</div>';

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
        container.innerHTML = `<div class="list-empty">${text}</div>`;
    }
}

/**
 * Finds the element carrying `attribute="value"`.
 *
 * Matching in JS rather than building an attribute selector: an extension is
 * caller-supplied, so interpolating it into a selector needs CSS.escape, which
 * jsdom does not implement. Comparing the attribute directly needs no escaping
 * and cannot be broken by a value containing selector syntax.
 */
function findByAttribute(attribute: string, value: string): Element | null {
    const candidates = document.querySelectorAll(`[${attribute}]`);
    for (const candidate of candidates) {
        if (candidate.getAttribute(attribute) === value) return candidate;
    }
    return null;
}

/** Opens or closes one card, without repainting the rest of the list. */
function toggleCard(extension: string): void {
    const entry = directoryEntries.find((candidate) => candidate.extension === extension);
    if (!entry) return;

    const body = findByAttribute('data-dir-body', extension);
    const title = findByAttribute('data-dir-toggle', extension);
    if (!body || !title) return;

    const open = expanded.has(extension);
    if (open) {
        expanded.delete(extension);
        body.classList.add('collapsed');
        body.innerHTML = '';
    } else {
        expanded.add(extension);
        body.innerHTML = cardBodyHtml(entry);
        body.classList.remove('collapsed');
    }

    title.setAttribute('aria-expanded', String(!open));
    title.querySelector('.card-chevron')?.classList.toggle('open', !open);
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
 * Wires the toolbar and list once. Uses delegation and addEventListener
 * rather than inline onclick, so this page adds no new CSP unsafe-inline debt.
 */
function attachDirectoryListeners(): void {
    if (listenersAttached) return;

    const searchInput = el('directory-search') as HTMLInputElement | null;
    const container = el('directory-list');
    if (!searchInput || !container) return;

    // Filtering is local, so render on every keystroke rather than debouncing.
    searchInput.addEventListener('input', renderDirectory);
    searchInput.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key === 'Escape') {
            searchInput.value = '';
            renderDirectory();
        }
    });

    el('directory-online-only')?.addEventListener('change', renderDirectory);

    // Sort order is a view preference rather than a filter, so it is not reset
    // by Clear filters and does not count towards the "n of m shown" summary.
    el('directory-sort')?.addEventListener('change', renderDirectory);

    el('directory-clear-filter')?.addEventListener('click', () => {
        searchInput.value = '';
        const checkbox = el('directory-online-only') as HTMLInputElement | null;
        if (checkbox) checkbox.checked = false;
        renderDirectory();
    });

    el('directory-refresh')?.addEventListener('click', () => {
        void loadPhoneBook();
    });

    container.addEventListener('click', (event: Event) => {
        const target = event.target as HTMLElement;

        const dial = target.closest('[data-dir-dial]');
        if (dial) {
            const extension = dial.getAttribute('data-dir-dial');
            if (extension) void dialExtension(extension);
            return;
        }

        const toggle = target.closest('[data-dir-toggle]');
        if (toggle) {
            const extension = toggle.getAttribute('data-dir-toggle');
            if (extension) toggleCard(extension);
        }
    });

    // The card title is a span with role="button", so it gets no key handling
    // for free the way a real button would.
    container.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        const toggle = (event.target as HTMLElement).closest('[data-dir-toggle]');
        if (!toggle) return;
        event.preventDefault();
        const extension = toggle.getAttribute('data-dir-toggle');
        if (extension) toggleCard(extension);
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
