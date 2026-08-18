/**
 * Call Routing page module.
 * Handles Find Me/Follow Me, time-based routing, webhooks, hot desking,
 * recording retention, and callback queue management.
 */

import { fetchWithTimeout, getAuthHeaders, getApiBaseUrl } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
import { confirmDelete } from '../ui/confirm.ts';
import { escapeHtml } from '../utils/html.ts';

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

interface FMFMDestination {
    number: string;
    ring_time?: number;
}

interface FMFMConfig {
    extension: string;
    mode: string;
    enabled?: boolean;
    destinations?: FMFMDestination[];
    updated_at?: string;
}

interface FMFMExtensionsResponse {
    extensions?: FMFMConfig[];
    count?: number;
}

interface TimeConditions {
    days_of_week?: number[];
    start_time?: string;
    end_time?: string;
    holidays?: boolean;
}

interface TimeRoutingRule {
    rule_id: string;
    name: string;
    destination: string;
    route_to: string;
    time_conditions?: TimeConditions;
    priority?: number;
    enabled?: boolean;
}

interface TimeRoutingRulesResponse {
    rules?: TimeRoutingRule[];
    count?: number;
}

interface Webhook {
    url: string;
    event_types?: string[];
    secret?: string;
    enabled?: boolean;
}

interface WebhooksResponse {
    subscriptions?: Webhook[];
}

interface HotDeskSession {
    extension: string;
    device_mac?: string;
    device_ip?: string;
    login_time?: string;
    active?: boolean;
}

interface HotDeskSessionsResponse {
    sessions?: HotDeskSession[];
}

interface RetentionMatchRules {
    media?: string;
    extensions?: string[];
    min_duration_seconds?: number;
    max_duration_seconds?: number;
}

interface RetentionPolicy {
    policy_id: string;
    name: string;
    description?: string;
    // Null means "inherit the fallback", which is not the same as zero days.
    audio_days: number | null;
    transcript_days: number | null;
    priority: number;
    match_rules?: RetentionMatchRules;
    enabled: boolean;
    origin?: string;
    catch_all?: boolean;
}

interface RetentionPoliciesResponse {
    policies?: RetentionPolicy[];
    fallback_audio_days?: number;
    fallback_transcript_days?: number;
}

interface RetentionStatisticsResponse {
    total_policies?: number;
    total_recordings?: number;
    deleted_count?: number;
    transcripts_deleted?: number;
    last_cleanup?: string;
    last_sweep_summary?: string | null;
    active_holds?: number;
    enabled?: boolean;
    dry_run?: boolean;
    fallback_audio_days?: number;
    fallback_transcript_days?: number;
}

interface CallbackEntry {
    callback_id: string;
    queue_id: string;
    caller_number: string;
    caller_name?: string;
    requested_at: string;
    callback_time: string;
    status: string;
    attempts: number;
}

interface CallbackListResponse {
    callbacks?: CallbackEntry[];
}

interface CallbackStatisticsResponse {
    total_callbacks?: number;
    status_breakdown?: Record<string, number>;
}

interface ApiSuccessResponse {
    success: boolean;
    error?: string;
    caller_number?: string;
}

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

let fmfmDestinationCounter = 0;

// ---------------------------------------------------------------------------
// Find Me / Follow Me
//
// The page draws each configuration as the ring plan it will actually produce,
// rather than as the fields it was stored with. That is the whole point of the
// view -- a config is an ordered plan, and "3 destinations" says nothing about
// what the caller hears -- but it means mirroring three rules from
// pbx/features/find_me_follow_me.py:
//
//   * plan_for() inserts the dialled extension's own phone as an implicit
//     first leg, unless initial_ring_time is 0 or the config lists the
//     extension itself somewhere (that placement wins, ring time included).
//   * _sanitize() drops blanks and duplicates and keeps at most
//     MAX_DESTINATIONS; plan_for() then truncates again *after* inserting the
//     desk leg, so the implicit leg consumes one of the ten.
//   * A simultaneous burst's total is its longest leg, not the sum: each leg
//     keeps its own ring time and drops out when that expires.
//
// Everything below marked "mirrors" is a duplication of that module and goes
// stale if it changes. It is duplicated rather than fetched because the plan is
// per-config and drawing it server-side would mean a request per row.
// ---------------------------------------------------------------------------

const FMFM_DEFAULT_RING_TIME = 20;

/**
 * The ring time a blank box stands for, which depends on the mode.
 *
 * Sequentially a ring time is a delay charged to every destination below it, so
 * the default is kept short. In a burst it delays nothing and only decides how
 * long that leg keeps trying, so there is no reason to offer less than the
 * ceiling -- and taking the ceiling from `fmfmBounds` rather than repeating its
 * number here means a deployment that raises `max_ring_time_simultaneous` moves
 * the default with it instead of silently diverging from it.
 *
 * This is a UI default, not a backend one: the resolved value is always sent
 * explicitly, so find_me_follow_me.py's DEFAULT_RING_TIME still governs a config
 * created through the API without a ring time.
 */
function defaultRingFor(mode: string): number {
    return mode === 'simultaneous' ? maxRingFor(mode) : FMFM_DEFAULT_RING_TIME;
}

const FMFM_LOAD_TIMEOUT = 10000;

/**
 * Mirrors MAX_DESTINATIONS and the ring-time bounds in find_me_follow_me.py.
 *
 * These are fetched rather than hardcoded because the simultaneous ceiling is
 * derived from `voicemail.no_answer_timeout`, which is deployment-specific and
 * invisible from here. The literals below are only the fallback for a failed
 * statistics call -- keep them in step with the module's own defaults.
 */
interface FMFMBounds {
    min: number;
    maxSequential: number;
    maxSimultaneous: number;
    maxDestinations: number;
}

const FMFM_FALLBACK_BOUNDS: FMFMBounds = {
    min: 5,
    maxSequential: 60,
    maxSimultaneous: 30,
    maxDestinations: 10,
};

let fmfmBounds: FMFMBounds = { ...FMFM_FALLBACK_BOUNDS };

/** A bound from the server, falling back when it is missing or nonsensical. */
const bound = (raw: unknown, fallback: number): number =>
    typeof raw === 'number' && Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : fallback;

/** The ceiling that applies in `mode`. */
const maxRingFor = (mode: string): number =>
    mode === 'simultaneous' ? fmfmBounds.maxSimultaneous : fmfmBounds.maxSequential;

interface FMFMStatistics {
    initial_ring_time?: number;
    min_ring_time?: number;
    max_ring_time_sequential?: number;
    max_ring_time_simultaneous?: number;
    max_destinations?: number;
}

interface RingLeg {
    number: string;
    ring: number;
    /** True for the desk leg the backend inserts, false for a stored destination. */
    derived: boolean;
    /** Seconds from the start of ringing that this leg occupies. */
    from: number;
    to: number;
}

/** One row of operator input, and what the backend will do with it. */
interface PlanRow {
    number: string;
    ring: number;
    /** null when the row survives; otherwise why it will be discarded. */
    drop: null | 'empty' | 'duplicate' | 'over-cap';
}

interface RingPlan {
    /** Input rows, in the order given. Only the editor needs these. */
    rows: PlanRow[];
    legs: RingLeg[];
    total: number;
    /** The desk leg could not be drawn because initial_ring_time is unknown. */
    deskUnknown: boolean;
    /** The list places the extension itself, so no implicit leg is added. */
    listsSelf: boolean;
}

/** Full unfiltered list, held so filtering never needs the network. */
let fmfmConfigs: FMFMConfig[] = [];

/** null when the statistics call failed, which is not the same as 0. */
let fmfmInitialRing: number | null = null;

let fmfmListenersAttached = false;

/** Extensions whose card body is open. Survives re-renders from filtering. */
const fmfmExpanded = new Set<string>();

const fmfmEl = (id: string): HTMLElement | null => document.getElementById(id);

const clampRing = (seconds: number, mode: string): number =>
    Math.max(fmfmBounds.min, Math.min(maxRingFor(mode), seconds));

/**
 * The number a ring-time field holds, or null if it does not hold one.
 *
 * The editor keeps the field's text verbatim rather than a parsed number, so
 * that a half-typed value is never rewritten under the operator -- rewriting
 * it was what made a two-digit entry so hard to complete. Everything that
 * needs a number goes through here, and null is what blocks the save.
 */
const parseRing = (raw: string): number | null => {
    const text = raw.trim();
    if (!/^\d{1,4}$/.test(text)) return null;
    return parseInt(text, 10);
};

/** Why a ring-time field cannot be saved, or null if it can. */
function ringError(raw: string, mode: string): string | null {
    const value = parseRing(raw);
    // A blank box is not a mistake: it stands for the mode's default, which the
    // field shows as a placeholder and which is what gets saved.
    if (value === null) return raw.trim() ? 'is not a whole number of seconds' : null;
    if (value < fmfmBounds.min) return `is below the ${fmfmBounds.min} s minimum`;
    if (value > maxRingFor(mode)) return `is above the ${maxRingFor(mode)} s maximum`;
    return null;
}

/**
 * The ring plan a set of destinations will actually produce. Mirrors
 * _sanitize() followed by plan_for(); see the note at the top of this section.
 *
 * Takes loose parts rather than an FMFMConfig so the configure dialog can call
 * it against an unsaved draft. One implementation for both, because two would
 * mean the read-only view and the editor could disagree about the same config
 * -- which is exactly the confusion the plan display exists to remove.
 */
function computePlan(
    extension: string,
    mode: string,
    destinations: { number: string; ring: number }[]
): RingPlan {
    const simultaneous = mode === 'simultaneous';

    // _sanitize(): well-formed, no duplicates, ring times clamped, capped. The
    // caller-self-reference guard is deliberately not mirrored -- it depends on
    // who is calling, so there is nothing to show ahead of a call.
    const seen = new Set<string>();
    const rows: PlanRow[] = [];
    for (const dest of destinations) {
        const number = dest.number.trim();
        const ring = clampRing(dest.ring, mode);
        if (!number) {
            rows.push({ number, ring, drop: 'empty' });
        } else if (seen.has(number)) {
            rows.push({ number, ring, drop: 'duplicate' });
        } else if (seen.size >= fmfmBounds.maxDestinations) {
            rows.push({ number, ring, drop: 'over-cap' });
        } else {
            seen.add(number);
            rows.push({ number, ring, drop: null });
        }
    }

    const kept = rows.filter((row) => !row.drop);
    const listsSelf = kept.some((row) => row.number === extension);

    // plan_for(): no destinations means the call routes normally, so there is
    // no plan at all -- not a plan consisting of just the desk.
    let legs: RingLeg[] = kept.map((row) => ({
        number: row.number, ring: row.ring, derived: false, from: 0, to: 0,
    }));
    let deskUnknown = false;

    if (kept.length > 0) {
        if (fmfmInitialRing === null) {
            deskUnknown = true;
        } else if (fmfmInitialRing > 0 && !listsSelf) {
            // In a burst the desk rings for at least as long as the longest
            // destination, so it is not silent while the call is still being
            // chased elsewhere.
            const ring = simultaneous
                ? clampRing(Math.max(fmfmInitialRing, ...legs.map((leg) => leg.ring)), mode)
                : fmfmInitialRing;
            legs = [{ number: extension, ring, derived: true, from: 0, to: 0 }, ...legs];
        }

        // The truncation happens *after* the desk leg is inserted, so the
        // implicit leg consumes one of the ten and the last stored destination
        // silently stops ringing. Marked on the input row so the editor can say
        // so rather than letting it be discovered on a live call.
        if (legs.length > fmfmBounds.maxDestinations) {
            const dropped = new Set(legs.slice(fmfmBounds.maxDestinations).map((leg) => leg.number));
            legs = legs.slice(0, fmfmBounds.maxDestinations);
            for (const row of rows) {
                if (!row.drop && dropped.has(row.number)) row.drop = 'over-cap';
            }
        }
    }

    let elapsed = 0;
    for (const leg of legs) {
        if (simultaneous) {
            leg.from = 0;
            leg.to = leg.ring;
        } else {
            leg.from = elapsed;
            leg.to = elapsed + leg.ring;
            elapsed = leg.to;
        }
    }

    const total = legs.length === 0
        ? 0
        : simultaneous
            ? Math.max(...legs.map((leg) => leg.ring))
            : legs.reduce((sum, leg) => sum + leg.ring, 0);

    return { rows, legs, total, deskUnknown, listsSelf };
}

/** The plan for a stored configuration. */
function ringPlan(config: FMFMConfig): RingPlan {
    return computePlan(
        config.extension,
        config.mode,
        (config.destinations ?? []).map((dest) => ({
            number: String(dest.number ?? ''),
            ring: dest.ring_time == null ? defaultRingFor(config.mode) : Number(dest.ring_time),
        }))
    );
}

/** What kind of thing a leg points at, for the line under the number. */
function legKind(leg: RingLeg, config: FMFMConfig): string {
    if (leg.derived) return "this extension's own phone, rung automatically";
    if (leg.number === config.extension) return 'this extension';
    return /^\d{1,6}$/.test(leg.number) ? 'extension' : 'external';
}

/**
 * Absolute local time rather than "2 days ago".
 *
 * updated_at comes from a PostgreSQL TIMESTAMP, which isoformat() serialises
 * without an offset, and JavaScript reads an offset-less ISO string as local
 * time. On a UTC server viewed from a browser behind UTC, a row saved a moment
 * ago therefore parses as being in the future, and a relative rendering would
 * say so. Showing the wall-clock value cannot be wrong in that way.
 */
function fmfmUpdated(raw: string | undefined): { text: string; title: string } {
    if (!raw) return { text: 'Never', title: 'No recorded change' };
    const when = new Date(raw);
    if (Number.isNaN(when.getTime())) return { text: '—', title: String(raw) };
    return {
        text: when.toLocaleString(undefined, {
            day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
        }),
        title: when.toLocaleString(),
    };
}

function ringPlanHtml(config: FMFMConfig): string {
    const plan = ringPlan(config);
    const simultaneous = config.mode === 'simultaneous';

    if (plan.legs.length === 0) {
        return `<div class="muted-note" style="padding: 4px 16px 12px;">
                    No destinations, so this configuration does nothing — the call
                    routes normally and reaches the extension's own voicemail.
                </div>`;
    }

    const lead = simultaneous
        ? `Ring plan — all at once, for ${plan.total} s`
        : 'Ring plan — one at a time';

    let position = 0;
    const steps = plan.legs.map((leg) => {
        position += 1;
        const mark = simultaneous
            ? '<span class="ring-index ring-index-all" aria-hidden="true">≡</span>'
            : `<span class="ring-index">${position}</span>`;
        const when = simultaneous ? `0 – ${leg.to} s` : `${leg.from} – ${leg.to} s`;
        return `
            <div class="ring-step">
                ${mark}
                <span class="ring-number">${escapeHtml(leg.number)}</span>
                <span class="ring-kind">${legKind(leg, config)}</span>
                <span class="ring-when">${when}</span>
            </div>`;
    }).join('');

    // Stated even though it is always the dialled extension's own mailbox,
    // because "where does this end up" is the question a plan raises and
    // leaving it unanswered invites the assumption that it is the last
    // destination tried.
    const tail = `
        <div class="ring-tail">
            <span class="ring-index" aria-hidden="true">↳</span>
            <span>No answer after ${plan.total} s → voicemail for ${escapeHtml(config.extension)}</span>
        </div>`;

    const unknown = plan.deskUnknown
        ? `<div class="muted-note" style="padding: 8px 0 0;">
               The extension's own phone also rings, before this list. Its length
               could not be read from the server, so it is not shown above and the
               total excludes it.
           </div>`
        : '';

    return `<div class="ring-plan">
                <div class="ring-lead">${lead}</div>
                ${steps}
                ${tail}
                ${unknown}
            </div>`;
}

function fmfmCardHtml(config: FMFMConfig): string {
    const ext = escapeHtml(config.extension);
    const open = fmfmExpanded.has(config.extension);
    const plan = ringPlan(config);
    const updated = fmfmUpdated(config.updated_at);

    // Only a genuine exception earns a pill. Active is the norm, and the mode
    // is a property rather than a status, so it sits with the other stats.
    const pill = config.enabled === false
        ? '<span class="pill pill-warn">Disabled</span>'
        : '';

    const mode = config.mode === 'simultaneous' ? 'Simultaneous' : 'Sequential';

    return `
        <div class="card-shell g g3 g-hover">
            <div class="card-head">
                <span class="card-title" role="button" tabindex="0"
                      aria-expanded="${open}" data-fmfm-toggle="${ext}">
                    <span class="card-chevron${open ? ' open' : ''}" aria-hidden="true">&#9654;</span>
                    <strong>Extension ${ext}</strong>
                </span>
                ${pill}
                <span class="meta-stats">
                    <span class="meta-stat"><span class="k">Mode</span><span class="v">${mode}</span></span>
                    <span class="meta-stat"><span class="k">Destinations</span><span class="v">${plan.legs.length}</span></span>
                    <span class="meta-stat"><span class="k">Total ring</span><span class="v">${plan.total} s</span></span>
                    <span class="meta-stat"><span class="k">Updated</span><span class="v" title="${escapeHtml(updated.title)}">${escapeHtml(updated.text)}</span></span>
                </span>
                <span class="card-actions">
                    <button type="button" class="btn-ghost" data-fmfm-edit="${ext}">Edit</button>
                    <button type="button" class="btn-ghost btn-ghost-danger" data-fmfm-delete="${ext}">Delete</button>
                </span>
            </div>
            <div class="card-body${open ? '' : ' collapsed'}" data-fmfm-body="${ext}">
                ${open ? ringPlanHtml(config) : ''}
            </div>
        </div>`;
}

/** Placeholder rows shown while the list loads. */
function fmfmSkeletonHtml(): string {
    const widths = [116, 132, 104, 124];
    const rows = widths.map((width) => `
        <div class="card-shell g g3 skeleton" aria-hidden="true">
            <div class="card-head">
                <span class="card-title">
                    <span class="sk-bar" style="width: ${width}px; height: 12px;"></span>
                </span>
                <span class="meta-stats">
                    <span class="meta-stat">
                        <span class="sk-bar" style="width: 30px; height: 8px;"></span>
                        <span class="sk-bar" style="width: 72px; height: 10px;"></span>
                    </span>
                    <span class="meta-stat">
                        <span class="sk-bar" style="width: 30px; height: 8px;"></span>
                        <span class="sk-bar" style="width: 20px; height: 10px;"></span>
                    </span>
                    <span class="meta-stat">
                        <span class="sk-bar" style="width: 30px; height: 8px;"></span>
                        <span class="sk-bar" style="width: 36px; height: 10px;"></span>
                    </span>
                </span>
                <span class="card-actions">
                    <span class="sk-bar" style="width: 46px; height: 27px; border-radius: 9px;"></span>
                    <span class="sk-bar" style="width: 58px; height: 27px; border-radius: 9px;"></span>
                </span>
            </div>
        </div>`).join('');

    return `<div class="card-stack">${rows}</div>
            <span class="sr-only" role="status">Loading Find Me/Follow Me configurations</span>`;
}

/** A mark, a headline, the likely cause, and the action that resolves it. */
function fmfmEmptyStateHtml(mark: string, heading: string, detail: string, action = ''): string {
    return `
        <div class="card-shell g g3 empty-state">
            <div class="empty-mark${mark === '⚠️' ? ' empty-mark-warn' : ''}" aria-hidden="true">${mark}</div>
            <h3>${heading}</h3>
            <p>${detail}</p>
            ${action ? `<div class="empty-actions">${action}</div>` : ''}
        </div>`;
}

function fmfmNeedle(): string {
    return ((fmfmEl('fmfm-search') as HTMLInputElement | null)?.value ?? '').trim().toLowerCase();
}

function fmfmModeFilter(): string {
    return (fmfmEl('fmfm-mode-filter') as HTMLSelectElement | null)?.value ?? 'all';
}

function fmfmActiveOnly(): boolean {
    return (fmfmEl('fmfm-active-only') as HTMLInputElement | null)?.checked ?? false;
}

/** Extension or any destination number contains the needle. */
function fmfmMatches(config: FMFMConfig, needle: string): boolean {
    if (config.extension.toLowerCase().includes(needle)) return true;
    return (config.destinations ?? []).some((dest) =>
        String(dest.number ?? '').toLowerCase().includes(needle));
}

function fmfmClearFilters(): void {
    const search = fmfmEl('fmfm-search') as HTMLInputElement | null;
    if (search) search.value = '';
    const mode = fmfmEl('fmfm-mode-filter') as HTMLSelectElement | null;
    if (mode) mode.value = 'all';
    const active = fmfmEl('fmfm-active-only') as HTMLInputElement | null;
    if (active) active.checked = false;
    renderFMFM();
}

function updateFMFMStats(): void {
    const active = fmfmConfigs.filter((cfg) => cfg.enabled !== false).length;
    // The plan's leg count, not the stored list's: it is what will actually
    // ring, and it is the number the expanded row shows.
    const destinations = fmfmConfigs.reduce((sum, cfg) => sum + ringPlan(cfg).legs.length, 0);

    const total = fmfmEl('fmfm-total-extensions');
    if (total) total.textContent = String(fmfmConfigs.length);
    const activeEl = fmfmEl('fmfm-active-count');
    if (activeEl) activeEl.textContent = String(active);
    const destEl = fmfmEl('fmfm-destinations');
    if (destEl) destEl.textContent = String(destinations);
}

/** Applies the current filters to the cached list and repaints. */
function renderFMFM(): void {
    const container = fmfmEl('fmfm-list');
    if (!container) return;

    const needle = fmfmNeedle();
    const mode = fmfmModeFilter();
    const activeOnly = fmfmActiveOnly();

    const visible = fmfmConfigs.filter((cfg) => {
        if (needle && !fmfmMatches(cfg, needle)) return false;
        if (mode !== 'all' && cfg.mode !== mode) return false;
        return !(activeOnly && cfg.enabled === false);
    });

    const filtering = Boolean(needle) || mode !== 'all' || activeOnly;

    const clearButton = fmfmEl('fmfm-clear-filter');
    if (clearButton) clearButton.hidden = !filtering;

    const countLabel = fmfmEl('fmfm-count');
    if (countLabel) {
        if (fmfmConfigs.length === 0) {
            countLabel.textContent = '';
        } else if (filtering) {
            countLabel.textContent = `${visible.length} of ${fmfmConfigs.length} shown`;
        } else {
            const plural = fmfmConfigs.length === 1 ? 'extension' : 'extensions';
            countLabel.textContent = `${fmfmConfigs.length} ${plural}`;
        }
    }

    if (visible.length === 0) {
        // Filtered-to-nothing has an obvious next action; a genuinely empty
        // list has a likely cause. They are not the same situation.
        container.innerHTML = filtering
            ? fmfmEmptyStateHtml(
                '🔍',
                'Nothing matches those filters',
                'No configured extension or destination number matches. Check the spelling, or widen the filters.',
                '<button type="button" class="btn-ghost" data-fmfm-clear>Clear filters</button>')
            : fmfmEmptyStateHtml(
                '🔀',
                'No extensions are using Find Me / Follow Me',
                "Nothing is configured, so every extension rings its own phone and falls through to its own voicemail. Add a configuration to start forwarding an extension to a mobile or a group of desks.",
                '<button type="button" class="btn-ghost btn-ghost-accent" data-fmfm-new>Configure extension</button>');
        return;
    }

    // Numeric so 999 sorts before 1000, which a plain string compare reverses.
    const sorted = [...visible].sort((a, b) =>
        a.extension.localeCompare(b.extension, undefined, { numeric: true }));

    container.innerHTML = `<div class="card-stack">${sorted.map(fmfmCardHtml).join('')}</div>`;
}

/** Opens or closes one card, without repainting the rest of the list. */
function toggleFMFMCard(extension: string): void {
    const config = fmfmConfigs.find((candidate) => candidate.extension === extension);
    if (!config) return;

    const body = findFMFMByAttribute('data-fmfm-body', extension);
    const title = findFMFMByAttribute('data-fmfm-toggle', extension);
    if (!body || !title) return;

    const open = fmfmExpanded.has(extension);
    if (open) {
        fmfmExpanded.delete(extension);
        body.classList.add('collapsed');
        body.innerHTML = '';
    } else {
        fmfmExpanded.add(extension);
        body.innerHTML = ringPlanHtml(config);
        body.classList.remove('collapsed');
    }
    title.setAttribute('aria-expanded', String(!open));
    title.querySelector('.card-chevron')?.classList.toggle('open', !open);
}

/**
 * Finds the element carrying `attribute="value"`.
 *
 * Matched in JS rather than as an attribute selector: an extension is
 * operator-supplied, so interpolating it needs CSS.escape, which jsdom does not
 * implement. Comparing the attribute directly cannot be broken by a value
 * containing selector syntax.
 */
function findFMFMByAttribute(attribute: string, value: string): Element | null {
    const candidates = document.querySelectorAll(`[${attribute}]`);
    for (const candidate of candidates) {
        if (candidate.getAttribute(attribute) === value) return candidate;
    }
    return null;
}

function attachFMFMListeners(): void {
    if (fmfmListenersAttached) return;

    const search = fmfmEl('fmfm-search') as HTMLInputElement | null;
    const container = fmfmEl('fmfm-list');
    if (!search || !container) return;

    // Filtering is local, so render on every keystroke rather than debouncing.
    search.addEventListener('input', renderFMFM);
    search.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key === 'Escape') {
            search.value = '';
            renderFMFM();
        }
    });

    fmfmEl('fmfm-mode-filter')?.addEventListener('change', renderFMFM);
    fmfmEl('fmfm-active-only')?.addEventListener('change', renderFMFM);
    fmfmEl('fmfm-clear-filter')?.addEventListener('click', fmfmClearFilters);
    fmfmEl('fmfm-refresh')?.addEventListener('click', () => {
        void loadFMFMExtensions();
    });
    fmfmEl('fmfm-add')?.addEventListener('click', () => {
        showAddFMFMModal();
    });

    container.addEventListener('click', (event: Event) => {
        const target = event.target as HTMLElement;

        // These live inside the list container because an empty state replaces
        // the rows rather than sitting beside them.
        if (target.closest('[data-fmfm-clear]')) {
            fmfmClearFilters();
            return;
        }
        if (target.closest('[data-fmfm-retry]')) {
            void loadFMFMExtensions();
            return;
        }
        if (target.closest('[data-fmfm-new]')) {
            showAddFMFMModal();
            return;
        }

        const edit = target.closest('[data-fmfm-edit]');
        if (edit) {
            const extension = edit.getAttribute('data-fmfm-edit');
            const config = fmfmConfigs.find((candidate) => candidate.extension === extension);
            if (config) editFMFMConfig(config);
            return;
        }

        const remove = target.closest('[data-fmfm-delete]');
        if (remove) {
            const extension = remove.getAttribute('data-fmfm-delete');
            if (extension) void deleteFMFMConfig(extension);
            return;
        }

        const toggle = target.closest('[data-fmfm-toggle]');
        if (toggle) {
            const extension = toggle.getAttribute('data-fmfm-toggle');
            if (extension) toggleFMFMCard(extension);
        }
    });

    // The card title is a span with role="button", so it gets no key handling
    // for free the way a real button would.
    container.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        const toggle = (event.target as HTMLElement).closest('[data-fmfm-toggle]');
        if (!toggle) return;
        event.preventDefault();
        const extension = toggle.getAttribute('data-fmfm-toggle');
        if (extension) toggleFMFMCard(extension);
    });

    fmfmListenersAttached = true;
}

export async function loadFMFMExtensions(): Promise<void> {
    const container = fmfmEl('fmfm-list');
    if (!container) return;

    attachFMFMListeners();
    container.innerHTML = fmfmSkeletonHtml();

    const API_BASE = getApiBaseUrl();

    // Two requests, in parallel. The configurations are the page; the
    // statistics call is only for initial_ring_time, which is deployment-wide
    // rather than per-config, and the plan cannot show the implicit desk leg
    // without it. A failure there degrades the plan rather than the page, so it
    // is handled separately from the one that matters.
    const statistics = fetchWithTimeout(`${API_BASE}/api/fmfm/statistics`, {
        headers: getAuthHeaders(),
    }, FMFM_LOAD_TIMEOUT)
        .then((response) => (response.ok ? response.json() : null))
        .then((data: FMFMStatistics | null) => {
            const raw = data?.initial_ring_time;
            fmfmInitialRing = typeof raw === 'number' && Number.isFinite(raw) ? raw : null;
            fmfmBounds = {
                min: bound(data?.min_ring_time, FMFM_FALLBACK_BOUNDS.min),
                maxSequential: bound(
                    data?.max_ring_time_sequential, FMFM_FALLBACK_BOUNDS.maxSequential),
                maxSimultaneous: bound(
                    data?.max_ring_time_simultaneous, FMFM_FALLBACK_BOUNDS.maxSimultaneous),
                maxDestinations: bound(
                    data?.max_destinations, FMFM_FALLBACK_BOUNDS.maxDestinations),
            };
        })
        .catch(() => {
            fmfmInitialRing = null;
            fmfmBounds = { ...FMFM_FALLBACK_BOUNDS };
        });

    try {
        const response = await fetchWithTimeout(`${API_BASE}/api/fmfm/extensions`, {
            headers: getAuthHeaders(),
        }, FMFM_LOAD_TIMEOUT);
        if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);

        const data: FMFMExtensionsResponse = await response.json();
        await statistics;

        fmfmConfigs = data.extensions ?? [];
        // A configuration that has gone away must not keep a stale open row.
        for (const extension of [...fmfmExpanded]) {
            if (!fmfmConfigs.some((cfg) => cfg.extension === extension)) {
                fmfmExpanded.delete(extension);
            }
        }

        updateFMFMStats();
        renderFMFM();
    } catch (error: unknown) {
        console.error('Error loading FMFM extensions:', error);
        const message = error instanceof Error ? error.message : String(error);
        // This used to render through the same path as "no results", which told
        // the operator nothing was configured when in fact nothing was asked.
        const detail = message === 'Request timed out'
            ? `The request timed out after ${FMFM_LOAD_TIMEOUT / 1000} seconds. The PBX may still be starting up.`
            : 'The configurations could not be reached. It may be a temporary network problem.';
        container.innerHTML = fmfmEmptyStateHtml(
            '⚠️',
            'Could not load the configurations',
            detail,
            '<button type="button" class="btn-ghost btn-ghost-accent" data-fmfm-retry>Try again</button>');
        showNotification('Error loading FMFM configurations', 'error');
    }
}

// ---------------------------------------------------------------------------
// The configure dialog
//
// The editor and the plan are one list rather than a form with a preview beside
// it. A ring time only means something in the company of the rows above it, so
// the window a leg will occupy sits on the row being edited: "20" is legible as
// "rings from 0:20 to 0:45" while it is being chosen. The rows the backend will
// discard stay visible and editable, dimmed and flagged, because deleting them
// on the operator's behalf would hide why they vanished.
// ---------------------------------------------------------------------------

interface FMFMDraft {
    /** A new configuration rather than an edit; decides whether Extension is editable. */
    creating: boolean;
    extension: string;
    mode: string;
    enabled: boolean;
    /**
     * `ring` is the field's text, not a number. Holding the parsed value meant
     * re-rendering the input with it on every keystroke, which rewrote a
     * half-typed entry -- clearing the box produced "0", and the next digit
     * landed beside it. The text is now left exactly as typed and validated on
     * the way out, so an out-of-range value is reported and blocks the save
     * rather than being silently corrected mid-keystroke.
     */
    destinations: { number: string; ring: string }[];
}

let fmfmDraft: FMFMDraft | null = null;
let fmfmDialogListenersAttached = false;

const fmfmDialog = (): HTMLElement | null => document.getElementById('add-fmfm-modal');

function openFMFMDialog(draft: FMFMDraft): void {
    fmfmDraft = draft;
    attachFMFMDialogListeners();

    const extInput = fmfmEl('fmfm-extension') as HTMLInputElement | null;
    if (extInput) {
        extInput.value = draft.extension;
        // An extension is the identity of the configuration, not a field of it:
        // the store is keyed by it, so changing it would create a second
        // configuration rather than move this one.
        extInput.readOnly = !draft.creating;
    }

    const note = fmfmEl('fmfm-extension-note');
    if (note) {
        note.textContent = draft.creating
            ? 'The extension a call must arrive on for these rules to apply. It cannot be changed afterwards.'
            : 'Fixed once the configuration exists. Delete and recreate it to move these rules to another extension.';
    }

    const enabled = fmfmEl('fmfm-enabled') as HTMLInputElement | null;
    if (enabled) enabled.checked = draft.enabled;

    for (const radio of document.querySelectorAll<HTMLInputElement>('input[name="fmfm-mode"]')) {
        radio.checked = radio.value === draft.mode;
    }

    const title = fmfmEl('fmfm-dialog-title');
    if (title) {
        title.textContent = draft.creating
            ? 'Configure Find Me / Follow Me'
            : `Find Me / Follow Me — extension ${draft.extension}`;
    }
    const sub = fmfmEl('fmfm-dialog-sub');
    if (sub) {
        sub.textContent = draft.creating
            ? 'Pick an extension, then the numbers a call to it should chase'
            : 'Ring a chain of numbers when this extension is called';
    }

    const remove = fmfmEl('fmfm-dialog-delete');
    if (remove) remove.hidden = draft.creating;

    const save = fmfmEl('fmfm-dialog-save');
    if (save) save.textContent = draft.creating ? 'Create configuration' : 'Save configuration';

    // `.active` rather than an inline display, so the shared Escape handler in
    // ui/tabs.ts closes this like every other modal. The previous version set
    // style.display directly, which that handler does not match -- so Escape
    // did nothing here.
    fmfmDialog()?.classList.add('active');
    renderFMFMDialog();

    // Focus the first thing that is actually editable.
    if (draft.creating) extInput?.focus();
    else (document.querySelector('#fmfm-destinations-list [data-fmfm-dest]') as HTMLElement | null)?.focus();
}

export function showAddFMFMModal(): void {
    openFMFMDialog({
        creating: true,
        extension: '',
        mode: 'sequential',
        enabled: true,
        destinations: [{ number: '', ring: '' }],
    });
}

export function closeAddFMFMModal(): void {
    fmfmDialog()?.classList.remove('active');
    fmfmDraft = null;
}

export function editFMFMConfig(config: FMFMConfig): void {
    openFMFMDialog({
        creating: false,
        extension: config.extension,
        mode: config.mode === 'simultaneous' ? 'simultaneous' : 'sequential',
        enabled: config.enabled !== false,
        destinations: (config.destinations ?? []).map((dest) => ({
            number: String(dest.number ?? ''),
            // Shown as stored, not as clamped. A config saved before the
            // ceiling came down still opens with its own number visible and
            // flagged, so the operator sees what has to change.
            ring: String(Number(dest.ring_time) || FMFM_DEFAULT_RING_TIME),
        })),
    });
}

/**
 * Appends an empty destination row.
 *
 * Kept exported because it is registered on window and the markup used to call
 * it directly; the dialog itself goes through the draft.
 */
export function addFMFMDestinationRow(): void {
    if (!fmfmDraft) return;
    fmfmDraft.destinations.push({ number: '', ring: '' });
    fmfmDestinationCounter += 1;
    renderFMFMDialog();
    const inputs = document.querySelectorAll<HTMLElement>('#fmfm-destinations-list [data-fmfm-dest]');
    inputs[inputs.length - 1]?.focus();
}

/** Repaints the plan list and the warnings under it from the current draft. */
function renderFMFMDialog(): void {
    const container = fmfmEl('fmfm-destinations-list');
    const warnings = fmfmEl('fmfm-dialog-warnings');
    if (!container || !fmfmDraft) return;

    const draft = fmfmDraft;
    const simultaneous = draft.mode === 'simultaneous';
    // The plan needs numbers; the fields hold text. A field that does not hold
    // a usable number is drawn at the default so the plan stays readable while
    // it is being fixed -- the warning below says the save is blocked.
    const plan = computePlan(draft.extension, draft.mode, draft.destinations.map((dest) => ({
        number: dest.number,
        ring: parseRing(dest.ring) ?? defaultRingFor(draft.mode),
    })));

    for (const option of document.querySelectorAll<HTMLElement>('.mode-opt')) {
        option.classList.toggle('selected', option.dataset.fmfmMode === draft.mode);
    }

    const indexClass = simultaneous ? 'ring-index ring-index-all' : 'ring-index';
    const mark = (position: number): string => simultaneous
        ? `<span class="${indexClass}" aria-hidden="true">≡</span>`
        : `<span class="${indexClass}">${position}</span>`;
    const window_ = (leg: RingLeg): string =>
        simultaneous ? `0 – ${leg.to} s` : `${leg.from} – ${leg.to} s`;

    let html = '';
    let position = 0;

    const desk = plan.legs.find((leg) => leg.derived);
    if (desk) {
        position += 1;
        html += `
            <div class="plan-row plan-row-derived">
                <span class="plan-grip" style="visibility: hidden;" aria-hidden="true">⠿</span>
                ${mark(position)}
                <span class="plan-static"><strong>${escapeHtml(draft.extension)}</strong> — this extension's own phone, rung automatically</span>
                <span class="plan-fixed">${desk.ring} s</span>
                <span class="ring-when">${window_(desk)}</span>
                <span style="flex: 0 0 74px;"></span>
            </div>`;
    }

    plan.rows.forEach((row, index) => {
        if (!row.drop) position += 1;
        // Straight off the draft, not off the plan: the plan holds the clamped
        // number, and echoing that back into the box is what used to overwrite
        // what was being typed.
        const rawRing = draft.destinations[index]?.ring ?? '';
        const badRing = ringError(rawRing, draft.mode) !== null;
        const leg = row.drop
            ? undefined
            : plan.legs.find((candidate) => !candidate.derived && candidate.number === row.number);
        const flag = row.drop === 'duplicate' ? 'duplicate'
            : row.drop === 'over-cap' ? 'over cap'
            : '';
        const trailing = row.drop
            ? `<span class="plan-drop-flag">${flag}</span>`
            : `<span class="ring-when">${leg ? window_(leg) : ''}</span>`;

        html += `
            <div class="plan-row${row.drop ? ' plan-row-dropped' : ''}">
                <button type="button" class="plan-grip" data-fmfm-grip="${index}"
                        aria-label="Reorder destination ${index + 1}. Use the arrow keys to move it.">⠿</button>
                ${row.drop ? `<span class="${indexClass}">–</span>` : mark(position)}
                <span class="plan-number">
                    <input type="text" value="${escapeHtml(row.number)}" data-fmfm-dest="${index}"
                           placeholder="Phone number or extension" autocomplete="off"
                           aria-label="Destination ${index + 1} number">
                </span>
                <span class="plan-ring${badRing ? ' plan-ring-invalid' : ''}">
                    <input type="text" inputmode="numeric" value="${escapeHtml(rawRing)}"
                           placeholder="${defaultRingFor(draft.mode)}"
                           data-fmfm-ring="${index}" autocomplete="off" size="4"
                           aria-invalid="${badRing ? 'true' : 'false'}"
                           aria-label="Destination ${index + 1} ring time in seconds, default ${defaultRingFor(draft.mode)}">
                    <span class="unit">s</span>
                </span>
                ${trailing}
                <button type="button" class="btn-ghost btn-ghost-danger" data-fmfm-remove="${index}"
                        aria-label="Remove destination ${index + 1}">Remove</button>
            </div>`;
    });

    if (plan.rows.length === 0) {
        html += `<div class="form-empty">No destinations yet. Without at least one, the call routes
                 normally — the extension rings its own phone and reaches its own voicemail.</div>`;
    }

    if (plan.legs.length > 0) {
        html += `
            <div class="plan-tail">
                <span class="ring-index" aria-hidden="true">↳</span>
                <span>No answer → voicemail for ${escapeHtml(draft.extension)}</span>
                <span class="grand">${plan.total} s</span>
            </div>`;
    }

    // The implicit desk leg consumes one of the ten, so the point at which
    // adding another becomes pointless is nine, not ten.
    const keptRows = plan.rows.filter((row) => !row.drop).length;
    const legRoom = plan.legs.length >= fmfmBounds.maxDestinations
        || keptRows >= fmfmBounds.maxDestinations;

    html += `
        <div class="plan-add">
            <button type="button" class="btn-ghost" id="fmfm-add-destination"${legRoom ? ' disabled' : ''}>Add destination</button>
            <span class="count">${plan.legs.length} of ${fmfmBounds.maxDestinations} legs</span>
        </div>`;

    container.innerHTML = html;

    if (!warnings) return;
    const notes: string[] = [];
    if (plan.rows.some((row) => row.drop === 'duplicate')) {
        notes.push('A destination is listed twice. Only the first occurrence is dialled — the duplicate is dropped when the call is placed.');
    }
    if (plan.rows.some((row) => row.drop === 'over-cap')) {
        notes.push(`Over the ${fmfmBounds.maxDestinations}-leg cap. The extension's own phone counts toward it, so the last destination will never be dialled.`);
    }
    if (plan.listsSelf) {
        notes.push(`This lists extension ${escapeHtml(draft.extension)} itself, so it is not also rung automatically first — your placement is used instead, including its ring time and its position in the order.`);
    }
    if (plan.deskUnknown) {
        notes.push("The extension's own phone also rings, before this list. Its length could not be read from the server, so the plan above omits it and the total is short by that much.");
    }
    // Reported per row and blocking, rather than clamped on the operator's
    // behalf. A silently corrected ring time is a config that does not do what
    // the screen said it would.
    const badRings = draft.destinations
        .map((dest, index) => ({ index, why: ringError(dest.ring, draft.mode) }))
        .filter((entry) => entry.why !== null);
    for (const entry of badRings) {
        notes.push(`Ring time for destination ${entry.index + 1} ${entry.why} — allowed range is
                    ${fmfmBounds.min}–${maxRingFor(draft.mode)} s${simultaneous
                        ? ', because in simultaneous mode the longest destination is the whole time the caller waits'
                        : ''}. Fix it to save.`);
    }
    if (!simultaneous && plan.total > 120) {
        notes.push(`The caller waits ${plan.total} s before reaching voicemail. Most hang up well before two minutes.`);
    }

    warnings.innerHTML = notes
        .map((note) => `<div class="form-warn"><span aria-hidden="true">⚠</span><span>${note}</span></div>`)
        .join('');

    // Only a bad ring time blocks the save. The other notes describe things the
    // backend handles deliberately (a duplicate is dropped, an over-cap row is
    // never dialled), so they warn without standing in the way.
    const save = fmfmEl('fmfm-dialog-save') as HTMLButtonElement | null;
    if (save) {
        save.disabled = badRings.length > 0;
        save.title = badRings.length > 0
            ? 'A ring time is out of range. Fix it to save.'
            : '';
    }
}

/** Moves a destination, keeping the moved row focused. */
function moveFMFMDestination(from: number, to: number): void {
    if (!fmfmDraft) return;
    if (to < 0 || to >= fmfmDraft.destinations.length || from === to) return;
    const [moved] = fmfmDraft.destinations.splice(from, 1);
    if (!moved) return;
    fmfmDraft.destinations.splice(to, 0, moved);
    renderFMFMDialog();
    (document.querySelector(`[data-fmfm-grip="${to}"]`) as HTMLElement | null)?.focus();
}

function attachFMFMDialogListeners(): void {
    if (fmfmDialogListenersAttached) return;
    const modal = fmfmDialog();
    const container = fmfmEl('fmfm-destinations-list');
    if (!modal || !container) return;

    fmfmEl('fmfm-dialog-close')?.addEventListener('click', closeAddFMFMModal);
    fmfmEl('fmfm-dialog-cancel')?.addEventListener('click', closeAddFMFMModal);

    // Clicking the scrim dismisses; clicking anywhere in the dialog must not.
    modal.addEventListener('click', (event: Event) => {
        if (event.target === modal) closeAddFMFMModal();
    });

    fmfmEl('fmfm-dialog-delete')?.addEventListener('click', () => {
        if (fmfmDraft) void deleteFMFMConfig(fmfmDraft.extension);
    });

    (fmfmEl('add-fmfm-form') as HTMLFormElement | null)
        ?.addEventListener('submit', (event: Event) => { void saveFMFMConfig(event); });

    const extInput = fmfmEl('fmfm-extension') as HTMLInputElement | null;
    extInput?.addEventListener('input', () => {
        // The plan names this extension in its desk leg and its voicemail line,
        // so both follow what is being typed.
        if (fmfmDraft) fmfmDraft.extension = extInput.value.trim();
        renderFMFMDialog();
    });

    fmfmEl('fmfm-enabled')?.addEventListener('change', (event: Event) => {
        if (fmfmDraft) fmfmDraft.enabled = (event.target as HTMLInputElement).checked;
    });

    for (const radio of document.querySelectorAll<HTMLInputElement>('input[name="fmfm-mode"]')) {
        radio.addEventListener('change', () => {
            // Switching mode re-times every leg, so the plan is redrawn rather
            // than relabelled: sequential windows chain, simultaneous all start
            // at zero, and the total changes from a sum to a maximum.
            if (fmfmDraft && radio.checked) fmfmDraft.mode = radio.value;
            renderFMFMDialog();
        });
    }

    container.addEventListener('input', (event: Event) => {
        if (!fmfmDraft) return;
        const target = event.target as HTMLInputElement;
        const dest = target.dataset.fmfmDest;
        const ring = target.dataset.fmfmRing;
        const row = fmfmDraft.destinations[Number(dest ?? ring)];
        if (!row) return;
        if (dest !== undefined) {
            row.number = target.value;
        } else if (ring !== undefined) {
            row.ring = target.value;
        } else {
            return;
        }
        // Repainting would move focus and drop the caret, so the row being typed
        // in is left alone and only the derived parts are refreshed around it.
        const caret = target.selectionStart;
        renderFMFMDialog();
        const restored = document.querySelector<HTMLInputElement>(
            dest !== undefined ? `[data-fmfm-dest="${dest}"]` : `[data-fmfm-ring="${ring}"]`);
        if (restored) {
            restored.focus();
            if (caret !== null && restored.type === 'text') restored.setSelectionRange(caret, caret);
        }
    });

    container.addEventListener('click', (event: Event) => {
        const target = event.target as HTMLElement;
        if (target.closest('#fmfm-add-destination')) {
            addFMFMDestinationRow();
            return;
        }
        const remove = target.closest('[data-fmfm-remove]');
        if (remove && fmfmDraft) {
            fmfmDraft.destinations.splice(Number(remove.getAttribute('data-fmfm-remove')), 1);
            renderFMFMDialog();
        }
    });

    // Reordering by keyboard as well as by pointer: order is the whole meaning
    // of a sequential plan, and drag alone would put it out of reach.
    container.addEventListener('keydown', (event: KeyboardEvent) => {
        const grip = (event.target as HTMLElement).closest('[data-fmfm-grip]');
        if (!grip) return;
        const from = Number(grip.getAttribute('data-fmfm-grip'));
        if (event.key === 'ArrowUp') {
            event.preventDefault();
            moveFMFMDestination(from, from - 1);
        } else if (event.key === 'ArrowDown') {
            event.preventDefault();
            moveFMFMDestination(from, from + 1);
        }
    });

    container.addEventListener('pointerdown', (event: PointerEvent) => {
        const grip = (event.target as HTMLElement).closest('[data-fmfm-grip]');
        if (!grip) return;
        event.preventDefault();
        let from = Number(grip.getAttribute('data-fmfm-grip'));

        const onMove = (move: PointerEvent): void => {
            const rows = [...document.querySelectorAll('.plan-row:not(.plan-row-derived)')];
            const over = rows.findIndex((row) => {
                const box = row.getBoundingClientRect();
                return move.clientY >= box.top && move.clientY <= box.bottom;
            });
            if (over >= 0 && over !== from) {
                moveFMFMDestination(from, over);
                from = over;
            }
        };
        const onUp = (): void => {
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);
        };
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp);
    });

    fmfmDialogListenersAttached = true;
}

export async function saveFMFMConfig(event: Event): Promise<void> {
    event.preventDefault();
    if (!fmfmDraft) return;

    const extension = fmfmDraft.extension.trim();
    if (!extension) {
        showNotification('An extension is required', 'error');
        (fmfmEl('fmfm-extension') as HTMLInputElement | null)?.focus();
        return;
    }

    // Sent as typed, not as the plan shows it. The backend sanitises on the way
    // in, and storing only the surviving rows would silently discard input the
    // operator can still see on screen -- the warnings already say what will be
    // dropped, and reopening shows the same rows and the same warnings.
    // Enforced here rather than only on the button, because pressing Enter in a
    // text field submits the form without going near it.
    const bad = fmfmDraft.destinations.findIndex(
        (dest) => ringError(dest.ring, fmfmDraft?.mode ?? 'sequential') !== null);
    if (bad !== -1) {
        const why = ringError(fmfmDraft.destinations[bad]?.ring ?? '', fmfmDraft.mode);
        showNotification(`Ring time for destination ${bad + 1} ${why}`, 'error');
        (document.querySelector(`[data-fmfm-ring="${bad}"]`) as HTMLElement | null)?.focus();
        return;
    }

    // Captured because the closure below cannot narrow the module-level draft.
    const draftMode = fmfmDraft.mode;
    const destinations: FMFMDestination[] = fmfmDraft.destinations
        .filter((dest) => dest.number.trim())
        .map((dest) => ({
            number: dest.number.trim(),
            // A blank box promised the placeholder's value, so that is what is
            // stored -- not the backend's own default, which is mode-blind.
            ring_time: parseRing(dest.ring) ?? defaultRingFor(draftMode),
        }));

    if (destinations.length === 0) {
        showNotification('At least one destination is required', 'error');
        return;
    }

    const configData: Record<string, unknown> = {
        extension,
        mode: fmfmDraft.mode,
        enabled: fmfmDraft.enabled,
        destinations,
    };

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/fmfm/config`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify(configData)
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification(`FMFM configured for extension ${extension}`, 'success');
            closeAddFMFMModal();
            void loadFMFMExtensions();
        } else {
            showNotification(data.error || 'Error configuring FMFM', 'error');
        }
    } catch (error: unknown) {
        console.error('Error saving FMFM config:', error);
        showNotification('Error saving FMFM configuration', 'error');
    }
}

export async function deleteFMFMConfig(extension: string): Promise<void> {
    if (!await confirmDelete(`Find Me / Follow Me for extension ${extension}`)) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/fmfm/config/${extension}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification(`FMFM configuration deleted for ${extension}`, 'success');
            // Harmless when the delete came from the list rather than the
            // dialog; the dialog cannot be left open over a config that is gone.
            if (fmfmDraft?.extension === extension) closeAddFMFMModal();
            void loadFMFMExtensions();
        } else {
            showNotification(data.error || 'Error deleting FMFM configuration', 'error');
        }
    } catch (error: unknown) {
        console.error('Error deleting FMFM config:', error);
        showNotification('Error deleting FMFM configuration', 'error');
    }
}

// ---------------------------------------------------------------------------
// Time-Based Routing
// ---------------------------------------------------------------------------

export function getScheduleDescription(conditions: TimeConditions): string {
    const parts: string[] = [];

    if (conditions.days_of_week) {
        const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
        const days = conditions.days_of_week.map(d => dayNames[d]).join(', ');
        parts.push(days);
    }

    if (conditions.start_time && conditions.end_time) {
        parts.push(`${conditions.start_time}-${conditions.end_time}`);
    }

    if (conditions.holidays === true) {
        parts.push('Holidays');
    } else if (conditions.holidays === false) {
        parts.push('Non-holidays');
    }

    return parts.length > 0 ? parts.join(' | ') : 'Always';
}

export async function loadTimeRoutingRules(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/time-routing/rules`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data: TimeRoutingRulesResponse = await response.json();
        if (data.rules) {
            // Update stats
            const totalEl = document.getElementById('time-routing-total') as HTMLElement | null;
            if (totalEl) totalEl.textContent = String(data.count || 0);

            const activeCount = data.rules.filter(r => r.enabled !== false).length;
            const businessCount = data.rules.filter(r =>
                r.name && (r.name.toLowerCase().includes('business') || r.name.toLowerCase().includes('hours'))
            ).length;
            const afterCount = data.rules.filter(r =>
                r.name && (r.name.toLowerCase().includes('after') || r.name.toLowerCase().includes('closed'))
            ).length;

            const activeEl = document.getElementById('time-routing-active') as HTMLElement | null;
            if (activeEl) activeEl.textContent = String(activeCount);

            const businessEl = document.getElementById('time-routing-business') as HTMLElement | null;
            if (businessEl) businessEl.textContent = String(businessCount);

            const afterEl = document.getElementById('time-routing-after') as HTMLElement | null;
            if (afterEl) afterEl.textContent = String(afterCount);

            // Update table
            const tbody = document.getElementById('time-routing-list') as HTMLElement | null;
            if (!tbody) return;

            if (data.rules.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align: center;">No time-based routing rules</td></tr>';
            } else {
                tbody.innerHTML = data.rules.map(rule => {
                    const enabled = rule.enabled !== false;
                    const statusBadge = enabled
                        ? '<span class="badge" style="background: #10b981;">Active</span>'
                        : '<span class="badge" style="background: #6b7280;">Disabled</span>';

                    const conditions = rule.time_conditions || {};
                    const schedule = getScheduleDescription(conditions);

                    return `
                        <tr>
                            <td><strong>${escapeHtml(rule.name)}</strong></td>
                            <td>${escapeHtml(rule.destination)}</td>
                            <td>${escapeHtml(rule.route_to)}</td>
                            <td><small>${escapeHtml(schedule)}</small></td>
                            <td>${rule.priority || 100}</td>
                            <td>${statusBadge}</td>
                            <td>
                                <button class="btn-small btn-danger" onclick="deleteTimeRoutingRule('${escapeHtml(rule.rule_id)}', '${escapeHtml(rule.name)}')">Delete</button>
                            </td>
                        </tr>
                    `;
                }).join('');
            }
        }
    } catch (error: unknown) {
        console.error('Error loading time routing rules:', error);
        showNotification('Error loading time routing rules', 'error');
    }
}

export function showAddTimeRuleModal(): void {
    const modal = document.getElementById('add-time-rule-modal') as HTMLElement | null;
    if (modal) modal.style.display = 'block';
}

export function closeAddTimeRuleModal(): void {
    const modal = document.getElementById('add-time-rule-modal') as HTMLElement | null;
    if (modal) modal.style.display = 'none';
    const form = document.getElementById('add-time-rule-form') as HTMLFormElement | null;
    if (form) form.reset();
}

export async function saveTimeRoutingRule(event: Event): Promise<void> {
    event.preventDefault();

    const name = (document.getElementById('time-rule-name') as HTMLInputElement).value;
    const destination = (document.getElementById('time-rule-destination') as HTMLInputElement).value;
    const routeTo = (document.getElementById('time-rule-route-to') as HTMLInputElement).value;
    const startTime = (document.getElementById('time-rule-start') as HTMLInputElement).value;
    const endTime = (document.getElementById('time-rule-end') as HTMLInputElement).value;
    const priority = parseInt((document.getElementById('time-rule-priority') as HTMLInputElement).value);
    const enabled = (document.getElementById('time-rule-enabled') as HTMLInputElement).checked;

    // Collect selected days
    const selectedDays = Array.from(
        document.querySelectorAll<HTMLInputElement>('input[name="time-rule-days"]:checked')
    ).map(cb => parseInt(cb.value));

    if (selectedDays.length === 0) {
        showNotification('Please select at least one day of the week', 'error');
        return;
    }

    const ruleData = {
        name: name,
        destination: destination,
        route_to: routeTo,
        priority: priority,
        enabled: enabled,
        time_conditions: {
            days_of_week: selectedDays,
            start_time: startTime,
            end_time: endTime
        }
    };

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/time-routing/rule`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify(ruleData)
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification(`Time routing rule "${name}" added successfully`, 'success');
            closeAddTimeRuleModal();
            loadTimeRoutingRules();
        } else {
            showNotification(data.error || 'Error adding time routing rule', 'error');
        }
    } catch (error: unknown) {
        console.error('Error saving time routing rule:', error);
        showNotification('Error saving time routing rule', 'error');
    }
}

export async function deleteTimeRoutingRule(ruleId: string, ruleName: string): Promise<void> {
    if (!confirm(`Are you sure you want to delete time routing rule "${ruleName}"?`)) {
        return;
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/time-routing/rule/${ruleId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification(`Time routing rule "${ruleName}" deleted`, 'success');
            loadTimeRoutingRules();
        } else {
            showNotification(data.error || 'Error deleting time routing rule', 'error');
        }
    } catch (error: unknown) {
        console.error('Error deleting time routing rule:', error);
        showNotification('Error deleting time routing rule', 'error');
    }
}

// ---------------------------------------------------------------------------
// Webhooks
// ---------------------------------------------------------------------------

export async function loadWebhooks(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/webhooks`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data: WebhooksResponse = await response.json();
        if (data.subscriptions) {
            const tbody = document.getElementById('webhooks-list') as HTMLElement | null;
            if (!tbody) return;

            if (data.subscriptions.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align: center;">No webhooks configured</td></tr>';
            } else {
                tbody.innerHTML = data.subscriptions.map(webhook => {
                    const enabled = webhook.enabled !== false;
                    const statusBadge = enabled
                        ? '<span class="badge" style="background: #10b981;">Active</span>'
                        : '<span class="badge" style="background: #6b7280;">Disabled</span>';

                    const events = webhook.event_types || [];
                    const eventList = events.join(', ');
                    const hasSecret = webhook.secret ? 'Yes' : 'No';

                    return `
                        <tr>
                            <td>
                                <div style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(webhook.url)}">
                                    ${escapeHtml(webhook.url)}
                                </div>
                            </td>
                            <td>
                                <div style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(eventList)}">
                                    <small>${escapeHtml(eventList)}</small>
                                </div>
                            </td>
                            <td>${hasSecret}</td>
                            <td>${statusBadge}</td>
                            <td>
                                <button class="btn-small btn-danger" onclick="deleteWebhook('${escapeHtml(webhook.url)}')">Delete</button>
                            </td>
                        </tr>
                    `;
                }).join('');
            }
        }
    } catch (error: unknown) {
        console.error('Error loading webhooks:', error);
        showNotification('Error loading webhooks', 'error');
    }
}

export function showAddWebhookModal(): void {
    const modal = document.getElementById('add-webhook-modal') as HTMLElement | null;
    if (modal) modal.style.display = 'block';
}

export function closeAddWebhookModal(): void {
    const modal = document.getElementById('add-webhook-modal') as HTMLElement | null;
    if (modal) modal.style.display = 'none';
    const form = document.getElementById('add-webhook-form') as HTMLFormElement | null;
    if (form) form.reset();
}

export async function addWebhook(event: Event): Promise<void> {
    event.preventDefault();

    const url = (document.getElementById('webhook-url') as HTMLInputElement).value;
    const secret = (document.getElementById('webhook-secret') as HTMLInputElement).value;
    const enabled = (document.getElementById('webhook-enabled') as HTMLInputElement).checked;

    // Collect selected events
    const selectedEvents = Array.from(
        document.querySelectorAll<HTMLInputElement>('input[name="webhook-events"]:checked')
    ).map(cb => cb.value);

    if (selectedEvents.length === 0) {
        showNotification('Please select at least one event type', 'error');
        return;
    }

    const webhookData: Record<string, unknown> = {
        url: url,
        event_types: selectedEvents,
        enabled: enabled
    };

    if (secret) {
        webhookData.secret = secret;
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/webhooks`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify(webhookData)
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification('Webhook added successfully', 'success');
            closeAddWebhookModal();
            loadWebhooks();
        } else {
            showNotification(data.error || 'Error adding webhook', 'error');
        }
    } catch (error: unknown) {
        console.error('Error adding webhook:', error);
        showNotification('Error adding webhook', 'error');
    }
}

export async function deleteWebhook(url: string): Promise<void> {
    if (!confirm(`Are you sure you want to delete webhook for ${url}?`)) {
        return;
    }

    const encodedUrl = encodeURIComponent(url);

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/webhooks/${encodedUrl}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification('Webhook deleted', 'success');
            loadWebhooks();
        } else {
            showNotification(data.error || 'Error deleting webhook', 'error');
        }
    } catch (error: unknown) {
        console.error('Error deleting webhook:', error);
        showNotification('Error deleting webhook', 'error');
    }
}

// ---------------------------------------------------------------------------
// Hot Desking
// ---------------------------------------------------------------------------

export function getDuration(startTime: Date): string {
    const now = new Date();
    const diff = now.getTime() - startTime.getTime();

    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));

    if (hours > 0) {
        return `${hours}h ${minutes}m`;
    }
    return `${minutes}m`;
}

export async function loadHotDeskSessions(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/hot-desk/sessions`, {
            headers: getAuthHeaders()
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data: HotDeskSessionsResponse = await response.json();
        if (data.sessions) {
            // Update stats
            const activeSessions = data.sessions.filter(s => s.active !== false);

            const activeEl = document.getElementById('hotdesk-active') as HTMLElement | null;
            if (activeEl) activeEl.textContent = String(activeSessions.length);

            const totalEl = document.getElementById('hotdesk-total') as HTMLElement | null;
            if (totalEl) totalEl.textContent = String(data.sessions.length);

            // Update table
            const tbody = document.getElementById('hotdesk-sessions-list') as HTMLElement | null;
            if (!tbody) return;

            if (activeSessions.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align: center;">No active hot desk sessions</td></tr>';
            } else {
                tbody.innerHTML = activeSessions.map(session => {
                    const loginTime = session.login_time ? new Date(session.login_time).toLocaleString() : 'N/A';
                    const duration = session.login_time ? getDuration(new Date(session.login_time)) : 'N/A';

                    return `
                        <tr>
                            <td><strong>${escapeHtml(session.extension)}</strong></td>
                            <td>${escapeHtml(session.device_mac || 'N/A')}</td>
                            <td>${escapeHtml(session.device_ip || 'N/A')}</td>
                            <td><small>${loginTime}</small></td>
                            <td>${duration}</td>
                            <td>
                                <button class="btn-small btn-warning" onclick="logoutHotDesk('${escapeHtml(session.extension)}')">Logout</button>
                            </td>
                        </tr>
                    `;
                }).join('');
            }
        }
    } catch (error: unknown) {
        console.error('Error loading hot desk sessions:', error);
        showNotification('Error loading hot desk sessions', 'error');
    }
}

export async function logoutHotDesk(extension: string): Promise<void> {
    if (!confirm(`Are you sure you want to log out extension ${extension} from hot desk?`)) {
        return;
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/hot-desk/logout`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({ extension: extension })
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification(`Extension ${extension} logged out`, 'success');
            loadHotDeskSessions();
        } else {
            showNotification(data.error || 'Error logging out', 'error');
        }
    } catch (error: unknown) {
        console.error('Error logging out hot desk:', error);
        showNotification('Error logging out hot desk', 'error');
    }
}

// ---------------------------------------------------------------------------
// Recording Retention
// ---------------------------------------------------------------------------

// Last load, kept so the edit modal can prefill without refetching.
let retentionPolicies: RetentionPolicy[] = [];

export async function loadRetentionPolicies(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const [policiesRes, statsRes] = await Promise.all([
            fetchWithTimeout(`${API_BASE}/api/recording-retention/policies`, {
                headers: getAuthHeaders()
            }),
            fetchWithTimeout(`${API_BASE}/api/recording-retention/statistics`, {
                headers: getAuthHeaders()
            })
        ]);

        const [policiesData, statsData]: [RetentionPoliciesResponse, RetentionStatisticsResponse] =
            await Promise.all([policiesRes.json(), statsRes.json()]);

        // Update stats
        if (statsData) {
            const policiesCount = document.getElementById('retention-policies-count') as HTMLElement | null;
            if (policiesCount) policiesCount.textContent = String(statsData.total_policies || 0);

            const recordings = document.getElementById('retention-recordings') as HTMLElement | null;
            if (recordings) recordings.textContent = String(statsData.total_recordings || 0);

            const deleted = document.getElementById('retention-deleted') as HTMLElement | null;
            if (deleted) deleted.textContent = String(statsData.deleted_count || 0);

            const lastCleanup = statsData.last_cleanup
                ? new Date(statsData.last_cleanup).toLocaleString()
                : 'Never';
            const cleanupEl = document.getElementById('retention-last-cleanup') as HTMLElement | null;
            if (cleanupEl) cleanupEl.textContent = lastCleanup;

            // The mode banner is the most important thing on this page. Without it, a
            // "0 deleted / never cleaned" reading looks like retention is working and has had
            // nothing to do -- when it usually means retention is disabled, or is in
            // report-only mode and will never delete anything at all.
            const banner = document.getElementById('retention-mode-banner') as HTMLElement | null;
            if (banner) {
                if (statsData.enabled === false) {
                    banner.className = 'alert-box warning';
                    banner.textContent =
                        'Retention is DISABLED. Nothing is being deleted, and these policies '
                        + 'do not apply. Set retention.enabled in config.yml to turn it on.';
                } else if (statsData.dry_run) {
                    banner.className = 'alert-box warning';
                    banner.textContent =
                        'Retention is in DRY RUN mode: it reports what it would delete and '
                        + 'deletes nothing. Policies below are evaluated but never acted on. '
                        + (statsData.last_sweep_summary
                            ? `Last sweep: ${statsData.last_sweep_summary}`
                            : 'No sweep has run yet.');
                } else {
                    banner.className = 'alert-box';
                    banner.textContent =
                        'Retention is ACTIVE and deleting on schedule. '
                        + (statsData.last_sweep_summary
                            ? `Last sweep: ${statsData.last_sweep_summary}`
                            : 'No sweep has run yet.');
                }
                if (statsData.active_holds) {
                    banner.textContent +=
                        ` ${statsData.active_holds} session(s) under legal hold are exempt.`;
                }
            }
        }

        // Update policies table
        if (policiesData && policiesData.policies) {
            retentionPolicies = policiesData.policies;
            const tbody = document.getElementById('retention-policies-list') as HTMLElement | null;
            if (!tbody) return;

            const fallbackAudio = policiesData.fallback_audio_days ?? 0;
            const fallbackTranscript = policiesData.fallback_transcript_days ?? 0;

            // A null period inherits the config fallback. Showing "0 days" there would read
            // as "delete immediately", which is the opposite of what it means.
            const period = (days: number | null, fallback: number): string =>
                days === null
                    ? `<em>inherits ${fallback}d</em>`
                    : `${days} days`;

            if (policiesData.policies.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align: center;">No retention policies configured</td></tr>';
            } else {
                tbody.innerHTML = policiesData.policies.map(policy => {
                    const rules = policy.match_rules || {};
                    const parts: string[] = [];
                    if (rules.media) parts.push(`media = ${rules.media}`);
                    if (rules.extensions && rules.extensions.length) {
                        parts.push(`ext in ${rules.extensions.join(', ')}`);
                    }
                    if (rules.min_duration_seconds !== undefined) {
                        parts.push(`≥ ${rules.min_duration_seconds}s`);
                    }
                    if (rules.max_duration_seconds !== undefined) {
                        parts.push(`≤ ${rules.max_duration_seconds}s`);
                    }
                    const applies = parts.length
                        ? escapeHtml(parts.join(' and '))
                        : '<strong>everything</strong> (catch-all)';

                    const state = policy.enabled ? '' : ' <small>(disabled)</small>';

                    // The seeded catch-all governs everything no other policy matches.
                    // Deleting it would hand that job back to config.yml, which this page
                    // never shows, so it is editable but not removable.
                    const isDefault = policy.policy_id === 'default';
                    const remove = isDefault
                        ? '<small>default</small>'
                        : `<button class="btn-small btn-danger" onclick="deleteRetentionPolicy('${escapeHtml(policy.policy_id)}', '${escapeHtml(policy.name)}')">Delete</button>`;

                    return `
                        <tr>
                            <td><strong>${escapeHtml(policy.name)}</strong>${state}<br><small>${escapeHtml(policy.policy_id)}</small></td>
                            <td>${period(policy.audio_days, fallbackAudio)}</td>
                            <td>${period(policy.transcript_days, fallbackTranscript)}</td>
                            <td><small>${applies}</small></td>
                            <td><small>${policy.priority}</small></td>
                            <td>
                                <button class="btn-small" onclick="editRetentionPolicy('${escapeHtml(policy.policy_id)}')">Edit</button>
                                ${remove}
                            </td>
                        </tr>
                    `;
                }).join('');
            }
        }
    } catch (error: unknown) {
        console.error('Error loading retention policies:', error);
        showNotification('Error loading retention policies', 'error');
    }
}

function retentionField(id: string): HTMLInputElement | HTMLSelectElement | null {
    return document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null;
}

/** Which policy the modal is editing, or '' when it is creating a new one. */
let editingRetentionPolicy = '';

export function showAddRetentionPolicyModal(): void {
    editingRetentionPolicy = '';
    const form = document.getElementById('add-retention-policy-form') as HTMLFormElement | null;
    if (form) form.reset();

    const name = retentionField('retention-policy-name') as HTMLInputElement | null;
    if (name) name.readOnly = false;

    const title = document.getElementById('retention-modal-title') as HTMLElement | null;
    if (title) title.textContent = 'Add Retention Policy';

    const modal = document.getElementById('add-retention-policy-modal') as HTMLElement | null;
    if (modal) modal.style.display = 'block';
}

export function editRetentionPolicy(policyId: string): void {
    const policy = retentionPolicies.find(p => p.policy_id === policyId);
    if (!policy) {
        showNotification('Policy not found; refresh and try again', 'error');
        return;
    }

    editingRetentionPolicy = policy.policy_id;

    const set = (id: string, value: string): void => {
        const field = retentionField(id);
        if (field) field.value = value;
    };

    set('retention-policy-name', policy.name);
    // Null means "inherit the fallback", so it must come back as an empty field rather
    // than as a zero, which would be read as "delete immediately".
    set('retention-audio-days', policy.audio_days === null ? '' : String(policy.audio_days));
    set(
        'retention-transcript-days',
        policy.transcript_days === null ? '' : String(policy.transcript_days)
    );
    set('retention-priority', String(policy.priority));

    const rules = policy.match_rules || {};
    set('retention-match-media', rules.media || '');
    set('retention-match-extensions', (rules.extensions || []).join(', '));
    set(
        'retention-match-max-duration',
        rules.max_duration_seconds === undefined ? '' : String(rules.max_duration_seconds)
    );

    // The id is derived from the name, so renaming would create a second policy rather than
    // rename this one.
    const name = retentionField('retention-policy-name') as HTMLInputElement | null;
    if (name) name.readOnly = true;

    const title = document.getElementById('retention-modal-title') as HTMLElement | null;
    if (title) title.textContent = `Edit Policy: ${policy.name}`;

    const modal = document.getElementById('add-retention-policy-modal') as HTMLElement | null;
    if (modal) modal.style.display = 'block';
}

export function closeAddRetentionPolicyModal(): void {
    const modal = document.getElementById('add-retention-policy-modal') as HTMLElement | null;
    if (modal) modal.style.display = 'none';
    const form = document.getElementById('add-retention-policy-form') as HTMLFormElement | null;
    if (form) form.reset();
    editingRetentionPolicy = '';
}

export async function addRetentionPolicy(event: Event): Promise<void> {
    event.preventDefault();

    const name = (document.getElementById('retention-policy-name') as HTMLInputElement).value;
    const audioRaw = (document.getElementById('retention-audio-days') as HTMLInputElement).value.trim();
    const transcriptRaw = (document.getElementById('retention-transcript-days') as HTMLInputElement).value.trim();
    const media = (document.getElementById('retention-match-media') as HTMLSelectElement).value;
    const extensionsInput = (document.getElementById('retention-match-extensions') as HTMLInputElement).value;
    const maxDurationRaw = (document.getElementById('retention-match-max-duration') as HTMLInputElement).value.trim();
    const priorityRaw = (document.getElementById('retention-priority') as HTMLInputElement).value.trim();

    // Validate input
    if (!name.match(/^[a-zA-Z0-9_\s-]+$/)) {
        showNotification('Policy name contains invalid characters', 'error');
        return;
    }

    // Blank means "inherit the fallback" rather than zero, so blanks are sent as null and
    // only non-blank values are range-checked.
    const parsePeriod = (raw: string, label: string): number | null | false => {
        if (!raw) return null;
        const days = parseInt(raw, 10);
        if (isNaN(days) || days < 1 || days > 3650) {
            showNotification(`${label} must be between 1 and 3650 days`, 'error');
            return false;
        }
        return days;
    };

    const audioDays = parsePeriod(audioRaw, 'Audio retention');
    if (audioDays === false) return;
    const transcriptDays = parsePeriod(transcriptRaw, 'Transcript retention');
    if (transcriptDays === false) return;

    if (audioDays === null && transcriptDays === null) {
        showNotification('Set at least one of audio or transcript retention', 'error');
        return;
    }

    const matchRules: Record<string, unknown> = {};
    if (media) matchRules.media = media;
    if (extensionsInput.trim()) {
        matchRules.extensions = extensionsInput.split(',').map(t => t.trim()).filter(t => t);
    }
    if (maxDurationRaw) {
        const seconds = parseFloat(maxDurationRaw);
        if (isNaN(seconds) || seconds < 0) {
            showNotification('Maximum duration must be a positive number of seconds', 'error');
            return;
        }
        matchRules.max_duration_seconds = seconds;
    }

    const policyData: Record<string, unknown> = {
        name: name,
        audio_days: audioDays,
        transcript_days: transcriptDays,
        match_rules: matchRules
    };

    // Without this the server derives an id from the name, so an edit would insert a
    // near-duplicate instead of updating the row.
    if (editingRetentionPolicy) policyData.policy_id = editingRetentionPolicy;

    if (priorityRaw) {
        const priority = parseInt(priorityRaw, 10);
        if (!isNaN(priority)) policyData.priority = priority;
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/recording-retention/policy`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify(policyData)
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification(
                `Retention policy "${name}" ${editingRetentionPolicy ? 'updated' : 'added'}`,
                'success'
            );
            closeAddRetentionPolicyModal();
            loadRetentionPolicies();
        } else {
            showNotification(data.error || 'Error adding retention policy', 'error');
        }
    } catch (error: unknown) {
        console.error('Error adding retention policy:', error);
        showNotification('Error adding retention policy', 'error');
    }
}

export async function deleteRetentionPolicy(policyId: string, policyName: string): Promise<void> {
    if (!confirm(`Are you sure you want to delete retention policy "${policyName}"?`)) {
        return;
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(
            `${API_BASE}/api/recording-retention/policy/${encodeURIComponent(policyId)}`,
            {
                method: 'DELETE',
                headers: getAuthHeaders()
            }
        );

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification(`Retention policy "${policyName}" deleted`, 'success');
            loadRetentionPolicies();
        } else {
            showNotification(data.error || 'Error deleting retention policy', 'error');
        }
    } catch (error: unknown) {
        console.error('Error deleting retention policy:', error);
        showNotification('Error deleting retention policy', 'error');
    }
}

// ---------------------------------------------------------------------------
// Callback Queue
// ---------------------------------------------------------------------------

export async function loadCallbackQueue(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const [listRes, statsRes] = await Promise.all([
            fetchWithTimeout(`${API_BASE}/api/callback-queue/list`, {
                headers: getAuthHeaders()
            }),
            fetchWithTimeout(`${API_BASE}/api/callback-queue/statistics`, {
                headers: getAuthHeaders()
            })
        ]);

        const [listData, statsData]: [CallbackListResponse, CallbackStatisticsResponse] =
            await Promise.all([listRes.json(), statsRes.json()]);

        // Update statistics
        if (statsData) {
            const totalEl = document.getElementById('callback-total') as HTMLElement | null;
            if (totalEl) totalEl.textContent = String(statsData.total_callbacks || 0);

            const statusBreakdown = statsData.status_breakdown || {};
            const scheduledEl = document.getElementById('callback-scheduled') as HTMLElement | null;
            if (scheduledEl) scheduledEl.textContent = String(statusBreakdown.scheduled || 0);

            const inProgressEl = document.getElementById('callback-in-progress') as HTMLElement | null;
            if (inProgressEl) inProgressEl.textContent = String(statusBreakdown.in_progress || 0);

            const completedEl = document.getElementById('callback-completed') as HTMLElement | null;
            if (completedEl) completedEl.textContent = String(statusBreakdown.completed || 0);

            const failedEl = document.getElementById('callback-failed') as HTMLElement | null;
            if (failedEl) failedEl.textContent = String(statusBreakdown.failed || 0);
        }

        // Update callback list table
        if (listData && listData.callbacks) {
            const tbody = document.getElementById('callback-list') as HTMLElement | null;
            if (!tbody) return;

            if (listData.callbacks.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" style="text-align: center;">No callbacks in queue</td></tr>';
            } else {
                tbody.innerHTML = listData.callbacks.map(callback => {
                    const requestedTime = new Date(callback.requested_at).toLocaleString();
                    const callbackTime = new Date(callback.callback_time).toLocaleString();

                    let statusClass = '';
                    switch (callback.status) {
                        case 'scheduled': statusClass = 'badge-info'; break;
                        case 'in_progress': statusClass = 'badge-warning'; break;
                        case 'completed': statusClass = 'badge-success'; break;
                        case 'failed': statusClass = 'badge-danger'; break;
                        case 'cancelled': statusClass = 'badge-secondary'; break;
                        default: statusClass = 'badge-info';
                    }

                    return `
                        <tr>
                            <td><code>${escapeHtml(callback.callback_id)}</code></td>
                            <td>${escapeHtml(callback.queue_id)}</td>
                            <td>
                                <strong>${escapeHtml(callback.caller_number)}</strong><br>
                                <small>${escapeHtml(callback.caller_name || 'N/A')}</small>
                            </td>
                            <td><small>${requestedTime}</small></td>
                            <td><small>${callbackTime}</small></td>
                            <td><span class="badge ${statusClass}">${escapeHtml(callback.status)}</span></td>
                            <td>${callback.attempts}</td>
                            <td>
                                ${callback.status === 'scheduled' ? `
                                    <button class="btn-small btn-primary" onclick="startCallback('${escapeHtml(callback.callback_id)}')">Start</button>
                                    <button class="btn-small btn-danger" onclick="cancelCallback('${escapeHtml(callback.callback_id)}')">Cancel</button>
                                ` : callback.status === 'in_progress' ? `
                                    <button class="btn-small btn-success" onclick="completeCallback('${escapeHtml(callback.callback_id)}', true)">Done</button>
                                    <button class="btn-small btn-warning" onclick="completeCallback('${escapeHtml(callback.callback_id)}', false)">Retry</button>
                                ` : '-'}
                            </td>
                        </tr>
                    `;
                }).join('');
            }
        }
    } catch (error: unknown) {
        console.error('Error loading callback queue:', error);
        showNotification('Error loading callback queue', 'error');
    }
}

export function showRequestCallbackModal(): void {
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.id = 'request-callback-modal';
    modal.innerHTML = `
        <div class="modal-content">
            <span class="close" onclick="closeRequestCallbackModal()">&times;</span>
            <h2>Request Callback</h2>
            <form id="request-callback-form" onsubmit="requestCallback(event)">
                <div class="form-group">
                    <label for="callback-queue-id">Queue ID: *</label>
                    <input type="text" id="callback-queue-id" required
                           placeholder="e.g., sales, support, general">
                </div>
                <div class="form-group">
                    <label for="callback-caller-number">Caller Number: *</label>
                    <input type="tel" id="callback-caller-number" required
                           placeholder="e.g., +1234567890">
                </div>
                <div class="form-group">
                    <label for="callback-caller-name">Caller Name:</label>
                    <input type="text" id="callback-caller-name"
                           placeholder="Optional">
                </div>
                <div class="form-group">
                    <label for="callback-preferred-time">Preferred Time:</label>
                    <input type="datetime-local" id="callback-preferred-time">
                    <small>Leave empty for ASAP callback</small>
                </div>
                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeRequestCallbackModal()">Cancel</button>
                    <button type="submit" class="btn btn-success">Request Callback</button>
                </div>
            </form>
        </div>
    `;
    document.body.appendChild(modal);
    modal.style.display = 'block';
}

export function closeRequestCallbackModal(): void {
    const modal = document.getElementById('request-callback-modal');
    if (modal) modal.remove();
}

export async function requestCallback(event: Event): Promise<void> {
    event.preventDefault();

    const queueId = (document.getElementById('callback-queue-id') as HTMLInputElement).value;
    const callerNumber = (document.getElementById('callback-caller-number') as HTMLInputElement).value;
    const callerName = (document.getElementById('callback-caller-name') as HTMLInputElement).value;
    const preferredTime = (document.getElementById('callback-preferred-time') as HTMLInputElement).value;

    const callbackData: Record<string, unknown> = {
        queue_id: queueId,
        caller_number: callerNumber
    };

    if (callerName) {
        callbackData.caller_name = callerName;
    }

    if (preferredTime) {
        callbackData.preferred_time = new Date(preferredTime).toISOString();
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/callback-queue/request`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify(callbackData)
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification('Callback requested successfully', 'success');
            closeRequestCallbackModal();
            loadCallbackQueue();
        } else {
            showNotification(data.error || 'Error requesting callback', 'error');
        }
    } catch (error: unknown) {
        console.error('Error requesting callback:', error);
        showNotification('Error requesting callback', 'error');
    }
}

export async function startCallback(callbackId: string): Promise<void> {
    const agentId = prompt('Enter your agent ID/extension:');
    if (!agentId) {
        return;
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/callback-queue/start`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                callback_id: callbackId,
                agent_id: agentId
            })
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification(`Started callback to ${data.caller_number ?? 'caller'}`, 'success');
            loadCallbackQueue();
        } else {
            showNotification(data.error || 'Error starting callback', 'error');
        }
    } catch (error: unknown) {
        console.error('Error starting callback:', error);
        showNotification('Error starting callback', 'error');
    }
}

export async function completeCallback(callbackId: string, success: boolean): Promise<void> {
    let notes = '';
    if (!success) {
        notes = prompt('Enter reason for failure (optional):') || '';
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/callback-queue/complete`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                callback_id: callbackId,
                success: success,
                notes: notes
            })
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification(success ? 'Callback completed' : 'Callback will be retried', 'success');
            loadCallbackQueue();
        } else {
            showNotification(data.error || 'Error completing callback', 'error');
        }
    } catch (error: unknown) {
        console.error('Error completing callback:', error);
        showNotification('Error completing callback', 'error');
    }
}

export async function cancelCallback(callbackId: string): Promise<void> {
    if (!confirm('Are you sure you want to cancel this callback request?')) {
        return;
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/callback-queue/cancel`, {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({
                callback_id: callbackId
            })
        });

        const data: ApiSuccessResponse = await response.json();
        if (data.success) {
            showNotification('Callback cancelled', 'success');
            loadCallbackQueue();
        } else {
            showNotification(data.error || 'Error cancelling callback', 'error');
        }
    } catch (error: unknown) {
        console.error('Error cancelling callback:', error);
        showNotification('Error cancelling callback', 'error');
    }
}

// ---------------------------------------------------------------------------
// Backward compatibility - register with window
// ---------------------------------------------------------------------------

(window as any).loadFMFMExtensions = loadFMFMExtensions;
(window as any).showAddFMFMModal = showAddFMFMModal;
(window as any).closeAddFMFMModal = closeAddFMFMModal;
(window as any).addFMFMDestinationRow = addFMFMDestinationRow;
(window as any).saveFMFMConfig = saveFMFMConfig;
(window as any).editFMFMConfig = editFMFMConfig;
(window as any).deleteFMFMConfig = deleteFMFMConfig;
(window as any).getScheduleDescription = getScheduleDescription;
(window as any).showAddTimeRuleModal = showAddTimeRuleModal;
(window as any).closeAddTimeRuleModal = closeAddTimeRuleModal;
(window as any).loadTimeRoutingRules = loadTimeRoutingRules;
(window as any).saveTimeRoutingRule = saveTimeRoutingRule;
(window as any).deleteTimeRoutingRule = deleteTimeRoutingRule;
(window as any).showAddWebhookModal = showAddWebhookModal;
(window as any).closeAddWebhookModal = closeAddWebhookModal;
(window as any).loadWebhooks = loadWebhooks;
(window as any).addWebhook = addWebhook;
(window as any).deleteWebhook = deleteWebhook;
(window as any).loadHotDeskSessions = loadHotDeskSessions;
(window as any).logoutHotDesk = logoutHotDesk;
(window as any).getDuration = getDuration;
(window as any).loadRetentionPolicies = loadRetentionPolicies;
(window as any).showAddRetentionPolicyModal = showAddRetentionPolicyModal;
(window as any).closeAddRetentionPolicyModal = closeAddRetentionPolicyModal;
(window as any).addRetentionPolicy = addRetentionPolicy;
(window as any).deleteRetentionPolicy = deleteRetentionPolicy;
(window as any).editRetentionPolicy = editRetentionPolicy;
(window as any).loadCallbackQueue = loadCallbackQueue;
(window as any).showRequestCallbackModal = showRequestCallbackModal;
(window as any).closeRequestCallbackModal = closeRequestCallbackModal;
(window as any).requestCallback = requestCallback;
(window as any).startCallback = startCallback;
(window as any).completeCallback = completeCallback;
(window as any).cancelCallback = cancelCallback;
