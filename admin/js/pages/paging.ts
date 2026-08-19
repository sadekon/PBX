/**
 * Paging page module.
 *
 * Zones, the destinations inside them, and the pages currently in flight.
 *
 * WHAT CHANGED, AND WHY
 *     This page used to manage "DAC devices" through `/api/paging/devices`, an endpoint
 *     that no longer exists. Hardware is not paging's to own: an ATA is a provisioned
 *     device with a vendor, a MAC, and a firmware, and paging only ever needed the
 *     extension one of its phone ports answers on. Keeping a second copy here meant two
 *     records of the same box, free to disagree. A destination now names an extension and
 *     nothing else, and the vendor it needs is joined in from Provisioning when a page runs.
 */

import { getApiBaseUrl, getAuthHeaders, fetchWithTimeout } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
import { escapeHtml } from '../utils/html.ts';

/** Long enough for a cold database, short enough that a dead backend still paints. */
const PAGING_LOAD_TIMEOUT = 15000;

/** Mirrors `AUTO_ANSWER_HEADERS` in pbx/features/paging.py. */
const AUTO_ANSWER_VENDORS: Record<string, string> = {
    cisco: 'Cisco',
    yealink: 'Yealink',
    zultys: 'Zultys',
    polycom: 'Polycom',
    grandstream: 'Grandstream',
    none: 'Nothing sent',
};

/** Every symbol a keypad can produce, matching VALID_DTMF_DIGITS server-side. */
const VALID_DTMF = /^[0-9*#ABCD]+$/;

interface PagingDestination {
    id: number;
    zone_id: number;
    kind: string;
    label?: string | null;
    enabled?: boolean;
    endpoint_extension?: string | null;
    auto_answer_override?: string | null;
    dtmf_sequence?: string | null;
    multicast_address?: string | null;
    multicast_port?: number | null;
}

interface PagingZone {
    id: number;
    extension: string;
    name: string;
    description?: string | null;
    enabled?: boolean;
    max_duration_seconds?: number | null;
    destinations?: PagingDestination[];
}

interface PagingStatus {
    enabled?: boolean;
    persistent?: boolean;
    zone_count?: number;
    destination_count?: number;
    active_page_count?: number;
    default_max_duration?: number;
}

interface ActivePageDestination {
    destination_id: number;
    name?: string;
    kind?: string;
    extension?: string | null;
    state?: string;
}

interface ActivePage {
    page_id: string;
    from_extension: string;
    zone_id: number;
    zone_extension: string;
    zone_name: string;
    call_id?: string;
    started_at: string;
    destinations?: ActivePageDestination[];
}

interface PagingEndpoint {
    extension: string;
    port?: number;
    vendor?: string | null;
    model?: string | null;
    mac_address?: string | null;
}

/** Draft held while the zone dialog is open; null when it is closed. */
interface ZoneDraft {
    creating: boolean;
    id: number | null;
    extension: string;
}

let pagingZones: PagingZone[] = [];
let activePages: ActivePage[] = [];
let pagingEndpoints: PagingEndpoint[] = [];
let pagingStatus: PagingStatus = {};

let zoneDraft: ZoneDraft | null = null;
/** Which zone a newly added destination belongs to. */
let destinationZoneId: number | null = null;

let pagingListenersAttached = false;

/** Zone ids whose destination list is open. Survives a repaint. */
const pagingExpanded = new Set<number>();

const pagingEl = (id: string): HTMLElement | null => document.getElementById(id);

// ---------------------------------------------------------------------------- formatting

/** "no limit" is a real setting here, not a missing one, so it is spelled out. */
function durationLabel(seconds: number | null | undefined): string {
    if (seconds === null || seconds === undefined) return `${pagingStatus.default_max_duration ?? 120} s`;
    if (seconds === 0) return 'No limit';
    if (seconds < 60) return `${seconds} s`;
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return rest ? `${minutes} m ${rest} s` : `${minutes} m`;
}

function elapsedLabel(startedAt: string): string {
    const started = Date.parse(startedAt);
    if (Number.isNaN(started)) return '-';
    const seconds = Math.max(0, Math.round((Date.now() - started) / 1000));
    if (seconds < 60) return `${seconds} s`;
    return `${Math.floor(seconds / 60)} m ${seconds % 60} s`;
}

/**
 * What the PBX will do about picking a speaker circuit.
 *
 * The empty case is the normal one and the one people get wrong, so it says who dials
 * rather than showing a blank.
 */
function circuitLabel(destination: PagingDestination): string {
    const sequence = (destination.dtmf_sequence ?? '').trim();
    return sequence ? escapeHtml(sequence) : 'Caller dials';
}

function autoAnswerLabel(destination: PagingDestination): string {
    const override = (destination.auto_answer_override ?? '').trim().toLowerCase();
    if (!override) return 'From vendor';
    return AUTO_ANSWER_VENDORS[override] ?? escapeHtml(override);
}

function destinationName(destination: PagingDestination): string {
    if (destination.label) return destination.label;
    if (destination.kind === 'multicast') {
        return `${destination.multicast_address ?? '?'}:${destination.multicast_port ?? '?'}`;
    }
    return `Extension ${destination.endpoint_extension ?? '?'}`;
}

// ---------------------------------------------------------------------------- rendering

/** The destination table inside an expanded zone. */
function destinationsHtml(zone: PagingZone): string {
    const destinations = zone.destinations ?? [];

    if (destinations.length === 0) {
        return `<div class="plan-empty">
                    <p>This zone reaches nothing. Dialling ${escapeHtml(zone.extension)} answers,
                       plays the failure tone, and hangs up.</p>
                    <button type="button" class="btn-ghost btn-ghost-accent"
                            data-paging-add-destination="${zone.id}">Add a destination</button>
                </div>`;
    }

    const rows = destinations.map((destination) => {
        const multicast = destination.kind === 'multicast';
        // Multicast is stored by the schema but nothing streams to it yet. Saying so on the
        // row is the only place an operator would find out before a page reaches no one.
        const pill = multicast
            ? '<span class="pill pill-warn">Not yet streamed</span>'
            : '';

        return `
            <div class="plan-row">
                <span class="plan-main">
                    <strong>${escapeHtml(destinationName(destination))}</strong>
                    ${pill}
                </span>
                <span class="meta-stats">
                    <span class="meta-stat"><span class="k">Extension</span><span class="v">${escapeHtml(destination.endpoint_extension ?? '-')}</span></span>
                    <span class="meta-stat"><span class="k">Circuit</span><span class="v">${circuitLabel(destination)}</span></span>
                    <span class="meta-stat"><span class="k">Auto-answer</span><span class="v">${autoAnswerLabel(destination)}</span></span>
                </span>
                <span class="card-actions">
                    <button type="button" class="btn-ghost btn-ghost-danger"
                            data-paging-delete-destination="${destination.id}">Remove</button>
                </span>
            </div>`;
    }).join('');

    return `<div class="ring-plan">
                ${rows}
                <div class="empty-actions" style="padding-top: 10px;">
                    <button type="button" class="btn-ghost"
                            data-paging-add-destination="${zone.id}">Add destination</button>
                </div>
            </div>`;
}

function zoneCardHtml(zone: PagingZone): string {
    const open = pagingExpanded.has(zone.id);
    const destinations = zone.destinations ?? [];
    const paging = activePages.some((page) => page.zone_id === zone.id);

    // Only exceptions earn a pill. A zone that is enabled and idle is the norm.
    const pills = [
        zone.enabled === false ? '<span class="pill pill-warn">Disabled</span>' : '',
        paging ? '<span class="pill pill-ok">Paging now</span>' : '',
        destinations.length === 0 ? '<span class="pill pill-warn">No destinations</span>' : '',
    ].join('');

    return `
        <div class="card-shell g g3 g-hover">
            <div class="card-head">
                <span class="card-title" role="button" tabindex="0"
                      aria-expanded="${open}" data-paging-toggle="${zone.id}">
                    <span class="card-chevron${open ? ' open' : ''}" aria-hidden="true">&#9654;</span>
                    <strong>${escapeHtml(zone.extension)}</strong>
                    <span class="card-sub">${escapeHtml(zone.name)}</span>
                </span>
                ${pills}
                <span class="meta-stats">
                    <span class="meta-stat"><span class="k">Destinations</span><span class="v">${destinations.length}</span></span>
                    <span class="meta-stat"><span class="k">Time limit</span><span class="v">${durationLabel(zone.max_duration_seconds)}</span></span>
                </span>
                <span class="card-actions">
                    <button type="button" class="btn-ghost" data-paging-edit="${zone.id}">Edit</button>
                    <button type="button" class="btn-ghost btn-ghost-danger" data-paging-delete="${zone.id}">Delete</button>
                </span>
            </div>
            <div class="card-body${open ? '' : ' collapsed'}" data-paging-body="${zone.id}">
                ${open ? destinationsHtml(zone) : ''}
            </div>
        </div>`;
}

/** Placeholder rows shown while the list loads. */
function pagingSkeletonHtml(): string {
    const widths = [96, 128, 108];
    const rows = widths.map((width) => `
        <div class="card-shell g g3 skeleton" aria-hidden="true">
            <div class="card-head">
                <span class="card-title">
                    <span class="sk-bar" style="width: ${width}px; height: 12px;"></span>
                </span>
                <span class="meta-stats">
                    <span class="meta-stat">
                        <span class="sk-bar" style="width: 30px; height: 8px;"></span>
                        <span class="sk-bar" style="width: 24px; height: 10px;"></span>
                    </span>
                    <span class="meta-stat">
                        <span class="sk-bar" style="width: 30px; height: 8px;"></span>
                        <span class="sk-bar" style="width: 44px; height: 10px;"></span>
                    </span>
                </span>
                <span class="card-actions">
                    <span class="sk-bar" style="width: 46px; height: 27px; border-radius: 9px;"></span>
                    <span class="sk-bar" style="width: 58px; height: 27px; border-radius: 9px;"></span>
                </span>
            </div>
        </div>`).join('');

    return `<div class="card-stack">${rows}</div>
            <span class="sr-only" role="status">Loading paging zones</span>`;
}

/** A mark, a headline, the likely cause, and the action that resolves it. */
function pagingEmptyStateHtml(mark: string, heading: string, detail: string, action = ''): string {
    return `
        <div class="card-shell g g3 empty-state">
            <div class="empty-mark${mark === '⚠️' ? ' empty-mark-warn' : ''}" aria-hidden="true">${mark}</div>
            <h3>${heading}</h3>
            <p>${detail}</p>
            ${action ? `<div class="empty-actions">${action}</div>` : ''}
        </div>`;
}

function pagingNeedle(): string {
    return ((pagingEl('paging-search') as HTMLInputElement | null)?.value ?? '').trim().toLowerCase();
}

function pagingEnabledOnly(): boolean {
    return (pagingEl('paging-enabled-only') as HTMLInputElement | null)?.checked ?? false;
}

/** Zone number, name, or any destination's extension or label contains the needle. */
function pagingMatches(zone: PagingZone, needle: string): boolean {
    if (zone.extension.toLowerCase().includes(needle)) return true;
    if (zone.name.toLowerCase().includes(needle)) return true;
    return (zone.destinations ?? []).some((destination) =>
        String(destination.endpoint_extension ?? '').toLowerCase().includes(needle)
        || String(destination.label ?? '').toLowerCase().includes(needle));
}

function pagingClearFilters(): void {
    const search = pagingEl('paging-search') as HTMLInputElement | null;
    if (search) search.value = '';
    const enabled = pagingEl('paging-enabled-only') as HTMLInputElement | null;
    if (enabled) enabled.checked = false;
    renderPagingZones();
}

function updatePagingStats(): void {
    const destinations = pagingZones.reduce(
        (sum, zone) => sum + (zone.destinations ?? []).length, 0);

    const zoneCount = pagingEl('paging-zone-count');
    if (zoneCount) zoneCount.textContent = String(pagingZones.length);
    const destCount = pagingEl('paging-destination-count');
    if (destCount) destCount.textContent = String(destinations);
    const activeCount = pagingEl('paging-active-count');
    if (activeCount) activeCount.textContent = String(activePages.length);
}

/** Applies the current filters to the cached list and repaints. */
function renderPagingZones(): void {
    const container = pagingEl('paging-list');
    if (!container) return;

    const needle = pagingNeedle();
    const enabledOnly = pagingEnabledOnly();

    const visible = pagingZones.filter((zone) => {
        if (needle && !pagingMatches(zone, needle)) return false;
        return !(enabledOnly && zone.enabled === false);
    });

    const filtering = Boolean(needle) || enabledOnly;

    const clearButton = pagingEl('paging-clear-filter');
    if (clearButton) clearButton.hidden = !filtering;

    const countLabel = pagingEl('paging-count');
    if (countLabel) {
        if (pagingZones.length === 0) {
            countLabel.textContent = '';
        } else if (filtering) {
            countLabel.textContent = `${visible.length} of ${pagingZones.length} shown`;
        } else {
            countLabel.textContent = `${pagingZones.length} ${pagingZones.length === 1 ? 'zone' : 'zones'}`;
        }
    }

    if (visible.length === 0) {
        // Switched off, filtered to nothing, and nothing configured are three different
        // situations with three different fixes; the status call is what tells them apart.
        if (pagingStatus.enabled === false) {
            container.innerHTML = pagingEmptyStateHtml(
                '⚠️',
                'Paging is switched off',
                'The server is running without the paging feature, so zones cannot be read or dialled. Enable features.paging in config.yml and restart the PBX.');
            return;
        }
        container.innerHTML = filtering
            ? pagingEmptyStateHtml(
                '🔍',
                'Nothing matches those filters',
                'No zone number, name, or destination matches. Check the spelling, or widen the filters.',
                '<button type="button" class="btn-ghost" data-paging-clear>Clear filters</button>')
            : pagingEmptyStateHtml(
                '📢',
                'No paging zones yet',
                'Nothing is configured, so no number reaches the overhead amplifiers. Add a zone, then give it the extension an ATA phone port answers on.',
                '<button type="button" class="btn-ghost btn-ghost-accent" data-paging-new>Add zone</button>');
        return;
    }

    // Numeric so 99 sorts before 100, which a plain string compare reverses.
    const sorted = [...visible].sort((a, b) =>
        a.extension.localeCompare(b.extension, undefined, { numeric: true }));

    container.innerHTML = `<div class="card-stack">${sorted.map(zoneCardHtml).join('')}</div>`;
}

/** The in-progress section, hidden entirely when nothing is paging. */
function renderActivePages(): void {
    const section = pagingEl('paging-active-section');
    const container = pagingEl('paging-active-list');
    if (!section || !container) return;

    if (activePages.length === 0) {
        section.hidden = true;
        container.innerHTML = '';
        return;
    }

    section.hidden = false;
    container.innerHTML = `<div class="card-stack">${activePages.map((page) => {
        const destinations = page.destinations ?? [];
        const up = destinations.filter((d) => d.state === 'answered').length;

        return `
            <div class="card-shell g g3">
                <div class="card-head">
                    <span class="card-title">
                        <strong>${escapeHtml(page.zone_name)}</strong>
                        <span class="card-sub">from ${escapeHtml(page.from_extension)}</span>
                    </span>
                    <span class="pill pill-ok">Live</span>
                    <span class="meta-stats">
                        <span class="meta-stat"><span class="k">Reached</span><span class="v">${up} of ${destinations.length}</span></span>
                        <span class="meta-stat"><span class="k">Running</span><span class="v">${elapsedLabel(page.started_at)}</span></span>
                    </span>
                    <span class="card-actions">
                        <button type="button" class="btn-ghost btn-ghost-danger"
                                data-paging-kill="${escapeHtml(page.page_id)}">End page</button>
                    </span>
                </div>
            </div>`;
    }).join('')}</div>`;
}

/** Opens or closes one zone's destination list, without repainting the rest. */
function togglePagingCard(zoneId: number): void {
    const zone = pagingZones.find((candidate) => candidate.id === zoneId);
    if (!zone) return;

    const body = findPagingByAttribute('data-paging-body', String(zoneId));
    const title = findPagingByAttribute('data-paging-toggle', String(zoneId));
    if (!body || !title) return;

    const open = pagingExpanded.has(zoneId);
    if (open) {
        pagingExpanded.delete(zoneId);
        body.classList.add('collapsed');
        body.innerHTML = '';
    } else {
        pagingExpanded.add(zoneId);
        body.innerHTML = destinationsHtml(zone);
        body.classList.remove('collapsed');
    }
    title.setAttribute('aria-expanded', String(!open));
    title.querySelector('.card-chevron')?.classList.toggle('open', !open);
}

/**
 * Finds the element carrying `attribute="value"`.
 *
 * Matched in JS rather than as an attribute selector, for the same reason the Find Me /
 * Follow Me list does it: jsdom has no CSS.escape, and a value containing selector syntax
 * cannot break a direct comparison.
 */
function findPagingByAttribute(attribute: string, value: string): Element | null {
    const candidates = document.querySelectorAll(`[${attribute}]`);
    for (const candidate of candidates) {
        if (candidate.getAttribute(attribute) === value) return candidate;
    }
    return null;
}

// ---------------------------------------------------------------------------- loading

export async function loadPagingData(): Promise<void> {
    const container = pagingEl('paging-list');
    if (!container) return;

    attachPagingListeners();
    container.innerHTML = pagingSkeletonHtml();

    const API_BASE = getApiBaseUrl();

    // Status and active pages colour the page; the zones are the page. A failure in either
    // of the first two degrades what is shown rather than replacing it with an error, so
    // they are handled apart from the one request that actually matters.
    const status = fetchWithTimeout(`${API_BASE}/api/paging/status`, {
        headers: getAuthHeaders(),
    }, PAGING_LOAD_TIMEOUT)
        .then((response) => (response.ok ? response.json() : null))
        .then((data: PagingStatus | null) => { pagingStatus = data ?? {}; })
        .catch(() => { pagingStatus = {}; });

    const active = fetchWithTimeout(`${API_BASE}/api/paging/active`, {
        headers: getAuthHeaders(),
    }, PAGING_LOAD_TIMEOUT)
        .then((response) => (response.ok ? response.json() : null))
        .then((data: { active_pages?: ActivePage[] } | null) => {
            activePages = data?.active_pages ?? [];
        })
        .catch(() => { activePages = []; });

    // Only the destination dialog reads this, but it is fetched with the page rather than
    // when the dialog opens: an operator who adds a destination straight after a refresh
    // would otherwise race the request and be told no phone ports exist.
    const endpoints = fetchWithTimeout(`${API_BASE}/api/paging/endpoints`, {
        headers: getAuthHeaders(),
    }, PAGING_LOAD_TIMEOUT)
        .then((response) => (response.ok ? response.json() : null))
        .then((data: { endpoints?: PagingEndpoint[] } | null) => {
            pagingEndpoints = data?.endpoints ?? [];
        })
        .catch(() => { pagingEndpoints = []; });

    try {
        const response = await fetchWithTimeout(`${API_BASE}/api/paging/zones`, {
            headers: getAuthHeaders(),
        }, PAGING_LOAD_TIMEOUT);
        if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);

        const data: { zones?: PagingZone[] } = await response.json();
        await Promise.all([status, active, endpoints]);

        pagingZones = data.zones ?? [];

        // Drop expansions for zones that are gone, or the set grows without bound across
        // refreshes and a recycled id would open a card the operator never touched.
        const live = new Set(pagingZones.map((zone) => zone.id));
        for (const id of [...pagingExpanded]) {
            if (!live.has(id)) pagingExpanded.delete(id);
        }

        updatePagingStats();
        renderActivePages();
        renderPagingZones();
    } catch (error: unknown) {
        console.error('Error loading paging zones:', error);
        container.innerHTML = pagingEmptyStateHtml(
            '⚠️',
            'Could not load paging zones',
            'The server did not answer. The zones themselves are unaffected — this is the admin page failing to read them.',
            '<button type="button" class="btn-ghost" data-paging-retry>Try again</button>');
    }
}

// ---------------------------------------------------------------------------- zone dialog

function zoneDialog(): HTMLElement | null {
    return pagingEl('add-paging-zone-modal');
}

export function showAddZoneModal(): void {
    openZoneDialog(null);
}

export function closeZoneModal(): void {
    zoneDialog()?.classList.remove('active');
    zoneDraft = null;
}

function openZoneDialog(zone: PagingZone | null): void {
    const dialog = zoneDialog();
    if (!dialog) return;

    zoneDraft = {
        creating: zone === null,
        id: zone?.id ?? null,
        extension: zone?.extension ?? '',
    };

    const title = pagingEl('paging-zone-dialog-title');
    if (title) title.textContent = zone ? `Zone ${zone.extension}` : 'New paging zone';

    const extension = pagingEl('paging-zone-extension') as HTMLInputElement | null;
    if (extension) {
        extension.value = zone?.extension ?? '';
        // The number is the zone's identity and the API has no route to change it. Editable
        // here it would silently do nothing on save, which is worse than being unavailable.
        extension.disabled = zone !== null;
    }
    const note = pagingEl('paging-zone-extension-note');
    if (note) {
        note.textContent = zone
            ? 'The number cannot be changed. Delete the zone and create another to renumber it.'
            : 'Must not be a number an extension already answers on. Fixed once the zone exists.';
    }

    const name = pagingEl('paging-zone-name') as HTMLInputElement | null;
    if (name) name.value = zone?.name ?? '';
    const description = pagingEl('paging-zone-description') as HTMLInputElement | null;
    if (description) description.value = zone?.description ?? '';
    const duration = pagingEl('paging-zone-max-duration') as HTMLInputElement | null;
    if (duration) {
        duration.value = zone?.max_duration_seconds === null || zone?.max_duration_seconds === undefined
            ? ''
            : String(zone.max_duration_seconds);
        duration.placeholder = String(pagingStatus.default_max_duration ?? 120);
    }
    const enabled = pagingEl('paging-zone-enabled') as HTMLInputElement | null;
    if (enabled) enabled.checked = zone?.enabled !== false;

    const deleteButton = pagingEl('paging-zone-dialog-delete');
    if (deleteButton) deleteButton.hidden = zone === null;

    dialog.classList.add('active');
    if (zone === null) extension?.focus();
    else name?.focus();
}

async function saveZone(event: Event): Promise<void> {
    event.preventDefault();
    if (!zoneDraft) return;

    const extension = ((pagingEl('paging-zone-extension') as HTMLInputElement | null)?.value ?? '').trim();
    const name = ((pagingEl('paging-zone-name') as HTMLInputElement | null)?.value ?? '').trim();
    const description = ((pagingEl('paging-zone-description') as HTMLInputElement | null)?.value ?? '').trim();
    const durationRaw = ((pagingEl('paging-zone-max-duration') as HTMLInputElement | null)?.value ?? '').trim();
    const enabled = (pagingEl('paging-zone-enabled') as HTMLInputElement | null)?.checked ?? true;

    if (!name) {
        showNotification('The zone needs a name', 'error');
        return;
    }
    if (zoneDraft.creating && !extension) {
        showNotification('The zone needs a number to dial', 'error');
        return;
    }

    // An empty box means "use the server's default", which is a different instruction from
    // any number the box could hold -- including 0, which means no limit at all.
    const maxDuration = durationRaw === '' ? null : Number(durationRaw);
    if (maxDuration !== null && (!Number.isFinite(maxDuration) || maxDuration < 0)) {
        showNotification('The time limit must be a positive number of seconds, or empty', 'error');
        return;
    }

    const API_BASE = getApiBaseUrl();
    const creating = zoneDraft.creating;
    const body: Record<string, unknown> = { name, description, enabled };
    if (creating) body.extension = extension;
    if (maxDuration !== null) body.max_duration_seconds = maxDuration;

    try {
        const response = await fetch(
            creating
                ? `${API_BASE}/api/paging/zones`
                : `${API_BASE}/api/paging/zones/${zoneDraft.id}`,
            {
                method: creating ? 'POST' : 'PUT',
                headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });

        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            // The server's own words: a number already in use names what it collided with,
            // which no message written here could.
            showNotification(data.error ?? `Could not save the zone (HTTP ${response.status})`, 'error');
            return;
        }

        showNotification(creating ? `Zone ${extension} created` : 'Zone saved', 'success');
        closeZoneModal();
        await loadPagingData();
    } catch (error: unknown) {
        console.error('Error saving paging zone:', error);
        showNotification('Could not reach the server to save the zone', 'error');
    }
}

export async function deletePagingZone(zoneId: number): Promise<void> {
    const zone = pagingZones.find((candidate) => candidate.id === zoneId);
    if (!zone) return;

    const count = (zone.destinations ?? []).length;
    const detail = count
        ? `\n\nIts ${count} destination${count === 1 ? '' : 's'} will be removed with it. The extensions themselves are not touched.`
        : '';
    if (!confirm(`Delete zone ${zone.extension} (${zone.name})?${detail}`)) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/paging/zones/${zoneId}`, {
            method: 'DELETE',
            headers: getAuthHeaders(),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            showNotification(data.error ?? `Could not delete the zone (HTTP ${response.status})`, 'error');
            return;
        }
        showNotification(`Zone ${zone.extension} deleted`, 'success');
        closeZoneModal();
        await loadPagingData();
    } catch (error: unknown) {
        console.error('Error deleting paging zone:', error);
        showNotification('Could not reach the server to delete the zone', 'error');
    }
}

// ---------------------------------------------------------------------- destination dialog

function destinationDialog(): HTMLElement | null {
    return pagingEl('add-paging-destination-modal');
}

export function closeDestinationModal(): void {
    destinationDialog()?.classList.remove('active');
    destinationZoneId = null;
}

function openDestinationDialog(zoneId: number): void {
    const dialog = destinationDialog();
    const zone = pagingZones.find((candidate) => candidate.id === zoneId);
    if (!dialog || !zone) return;

    destinationZoneId = zoneId;

    const subtitle = pagingEl('paging-destination-dialog-sub');
    if (subtitle) {
        subtitle.textContent = `An amplifier input that ${zone.extension} speaks through`;
    }

    const select = pagingEl('paging-destination-endpoint') as HTMLSelectElement | null;
    const note = pagingEl('paging-destination-endpoint-note');
    if (select) {
        // An extension already in this zone would be INVITEd twice and the second leg would
        // collide with the first; the database rejects it, so it is not offered.
        const taken = new Set((zone.destinations ?? [])
            .map((destination) => destination.endpoint_extension)
            .filter((extension): extension is string => Boolean(extension)));
        const available = pagingEndpoints.filter((endpoint) => !taken.has(endpoint.extension));

        if (available.length === 0) {
            select.innerHTML = '<option value="">No phone ports available</option>';
            if (note) {
                note.textContent = pagingEndpoints.length === 0
                    ? 'No ATAs are provisioned. Add one under Provisioning with its device type set to ATA, and its phone port extensions will appear here.'
                    : 'Every provisioned phone port is already a destination in this zone.';
            }
        } else {
            select.innerHTML = available.map((endpoint) => {
                const hardware = [endpoint.vendor, endpoint.model].filter(Boolean).join(' ');
                const port = endpoint.port ? ` · port ${endpoint.port}` : '';
                const label = hardware ? ` — ${hardware}${port}` : port;
                return `<option value="${escapeHtml(endpoint.extension)}">${escapeHtml(endpoint.extension)}${escapeHtml(label)}</option>`;
            }).join('');
            if (note) {
                note.textContent = "The extension the ATA's phone port answers on. Each port drives one amplifier, so a two-port ATA offers two.";
            }
        }
    }

    const label = pagingEl('paging-destination-label') as HTMLInputElement | null;
    if (label) label.value = '';
    const dtmf = pagingEl('paging-destination-dtmf') as HTMLInputElement | null;
    if (dtmf) dtmf.value = '';
    const autoAnswer = pagingEl('paging-destination-auto-answer') as HTMLSelectElement | null;
    if (autoAnswer) autoAnswer.value = '';

    dialog.classList.add('active');
    select?.focus();
}

async function saveDestination(event: Event): Promise<void> {
    event.preventDefault();
    if (destinationZoneId === null) return;

    const extension = ((pagingEl('paging-destination-endpoint') as HTMLSelectElement | null)?.value ?? '').trim();
    const label = ((pagingEl('paging-destination-label') as HTMLInputElement | null)?.value ?? '').trim();
    const dtmf = ((pagingEl('paging-destination-dtmf') as HTMLInputElement | null)?.value ?? '').trim();
    const autoAnswer = ((pagingEl('paging-destination-auto-answer') as HTMLSelectElement | null)?.value ?? '').trim();

    if (!extension) {
        showNotification('Choose the phone port this destination reaches', 'error');
        return;
    }
    // Checked here as well as on the server: a bad sequence would otherwise be stored and
    // then play as silence, selecting nothing, with no sign of why.
    if (dtmf && !VALID_DTMF.test(dtmf)) {
        showNotification('A circuit selection can only contain 0-9, * and #', 'error');
        return;
    }

    const body: Record<string, unknown> = {
        kind: 'sip_endpoint',
        endpoint_extension: extension,
    };
    if (label) body.label = label;
    if (dtmf) body.dtmf_sequence = dtmf;
    if (autoAnswer) body.auto_answer_override = autoAnswer;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(
            `${API_BASE}/api/paging/zones/${destinationZoneId}/destinations`, {
                method: 'POST',
                headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });

        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            showNotification(data.error ?? `Could not add the destination (HTTP ${response.status})`, 'error');
            return;
        }

        // Open the zone that just gained a destination, so the result is visible without
        // hunting for the card and expanding it.
        pagingExpanded.add(destinationZoneId);
        showNotification(`Destination ${extension} added`, 'success');
        closeDestinationModal();
        await loadPagingData();
    } catch (error: unknown) {
        console.error('Error adding paging destination:', error);
        showNotification('Could not reach the server to add the destination', 'error');
    }
}

export async function deletePagingDestination(destinationId: number): Promise<void> {
    const zone = pagingZones.find((candidate) =>
        (candidate.destinations ?? []).some((destination) => destination.id === destinationId));
    const destination = (zone?.destinations ?? []).find((d) => d.id === destinationId);
    if (!destination) return;

    if (!confirm(`Remove ${destinationName(destination)} from zone ${zone?.extension}?`)) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/paging/destinations/${destinationId}`, {
            method: 'DELETE',
            headers: getAuthHeaders(),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            showNotification(data.error ?? `Could not remove the destination (HTTP ${response.status})`, 'error');
            return;
        }
        if (zone) pagingExpanded.add(zone.id);
        showNotification('Destination removed', 'success');
        await loadPagingData();
    } catch (error: unknown) {
        console.error('Error removing paging destination:', error);
        showNotification('Could not reach the server to remove the destination', 'error');
    }
}

// ---------------------------------------------------------------------------- active pages

export async function endActivePage(pageId: string): Promise<void> {
    const page = activePages.find((candidate) => candidate.page_id === pageId);
    if (!page) return;

    if (!confirm(`End the page from ${page.from_extension} to ${page.zone_name}?`)) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/paging/active/${encodeURIComponent(pageId)}`, {
            method: 'DELETE',
            headers: getAuthHeaders(),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            showNotification(data.error ?? `Could not end the page (HTTP ${response.status})`, 'error');
            return;
        }
        showNotification('Page ended', 'success');
        await loadPagingData();
    } catch (error: unknown) {
        console.error('Error ending page:', error);
        showNotification('Could not reach the server to end the page', 'error');
    }
}

// ---------------------------------------------------------------------------- listeners

function attachPagingListeners(): void {
    if (pagingListenersAttached) return;

    const search = pagingEl('paging-search') as HTMLInputElement | null;
    const container = pagingEl('paging-list');
    if (!search || !container) return;

    // Filtering is local, so render on every keystroke rather than debouncing.
    search.addEventListener('input', renderPagingZones);
    search.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key === 'Escape') {
            search.value = '';
            renderPagingZones();
        }
    });

    pagingEl('paging-enabled-only')?.addEventListener('change', renderPagingZones);
    pagingEl('paging-clear-filter')?.addEventListener('click', pagingClearFilters);
    pagingEl('paging-refresh')?.addEventListener('click', () => { void loadPagingData(); });
    pagingEl('paging-add-zone')?.addEventListener('click', () => { showAddZoneModal(); });

    container.addEventListener('click', (event: Event) => {
        const target = event.target as HTMLElement;

        // These live inside the list container because an empty state replaces the cards
        // rather than sitting beside them.
        if (target.closest('[data-paging-clear]')) {
            pagingClearFilters();
            return;
        }
        if (target.closest('[data-paging-retry]')) {
            void loadPagingData();
            return;
        }
        if (target.closest('[data-paging-new]')) {
            showAddZoneModal();
            return;
        }

        const addDestination = target.closest('[data-paging-add-destination]');
        if (addDestination) {
            openDestinationDialog(Number(addDestination.getAttribute('data-paging-add-destination')));
            return;
        }

        const removeDestination = target.closest('[data-paging-delete-destination]');
        if (removeDestination) {
            void deletePagingDestination(
                Number(removeDestination.getAttribute('data-paging-delete-destination')));
            return;
        }

        const edit = target.closest('[data-paging-edit]');
        if (edit) {
            const zoneId = Number(edit.getAttribute('data-paging-edit'));
            const zone = pagingZones.find((candidate) => candidate.id === zoneId);
            if (zone) openZoneDialog(zone);
            return;
        }

        const remove = target.closest('[data-paging-delete]');
        if (remove) {
            void deletePagingZone(Number(remove.getAttribute('data-paging-delete')));
            return;
        }

        const toggle = target.closest('[data-paging-toggle]');
        if (toggle) {
            togglePagingCard(Number(toggle.getAttribute('data-paging-toggle')));
        }
    });

    // A card title is a button by role, so it has to answer the keyboard like one.
    container.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        const toggle = (event.target as HTMLElement).closest('[data-paging-toggle]');
        if (!toggle) return;
        event.preventDefault();
        togglePagingCard(Number(toggle.getAttribute('data-paging-toggle')));
    });

    pagingEl('paging-active-list')?.addEventListener('click', (event: Event) => {
        const kill = (event.target as HTMLElement).closest('[data-paging-kill]');
        if (kill) void endActivePage(kill.getAttribute('data-paging-kill') ?? '');
    });

    pagingEl('add-paging-zone-form')?.addEventListener('submit', (event) => { void saveZone(event); });
    pagingEl('paging-zone-dialog-close')?.addEventListener('click', closeZoneModal);
    pagingEl('paging-zone-dialog-cancel')?.addEventListener('click', closeZoneModal);
    pagingEl('paging-zone-dialog-delete')?.addEventListener('click', () => {
        if (zoneDraft?.id !== null && zoneDraft?.id !== undefined) {
            void deletePagingZone(zoneDraft.id);
        }
    });

    pagingEl('add-paging-destination-form')?.addEventListener('submit', (event) => {
        void saveDestination(event);
    });
    pagingEl('paging-destination-dialog-close')?.addEventListener('click', closeDestinationModal);
    pagingEl('paging-destination-dialog-cancel')?.addEventListener('click', closeDestinationModal);

    // Escape closes whichever dialog is open, matching every other dialog on the site.
    document.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key !== 'Escape') return;
        if (destinationDialog()?.classList.contains('active')) closeDestinationModal();
        else if (zoneDialog()?.classList.contains('active')) closeZoneModal();
    });

    pagingListenersAttached = true;
}

// Registered on window because the tab switcher dispatches by name.
window.loadPagingData = loadPagingData;
window.showAddZoneModal = showAddZoneModal;
window.closeZoneModal = closeZoneModal;
window.closeDestinationModal = closeDestinationModal;
window.deletePagingZone = deletePagingZone;
window.deletePagingDestination = deletePagingDestination;
window.endActivePage = endActivePage;
