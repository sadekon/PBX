/**
 * Recording Analytics.
 *
 * Replaces the Speech Analytics page, whose module was removed: its live half never had a
 * feeder and never ran, and its post-call half duplicated this one. Analysis here reads the
 * transcript post-call transcription already produced -- it never opens audio.
 */

import { getApiBaseUrl, getAuthHeaders, fetchWithTimeout } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
import { escapeHtml } from '../utils/html.ts';

interface AnalysisSections {
    sentiment?: { overall_sentiment?: string; sentiment_score?: number };
    summary?: { summary?: string };
    keywords?: { keywords?: string[] };
    quality?: { overall_score?: number };
}

interface AnalysisRow {
    recording_id?: number | string;
    transcript_id?: number;
    analyzed_at?: string;
    analyses?: AnalysisSections;
}

interface AnalysesResponse {
    analyses?: AnalysisRow[];
}

interface AnalyticsStats {
    total_analyses?: number;
    analyses_by_type?: Record<string, number>;
    auto_analyze?: boolean;
}

function el(id: string): HTMLElement | null {
    return document.getElementById(id);
}

function render(rows: AnalysisRow[]): void {
    const tbody = el('analytics-table');
    if (!tbody) return;

    if (!rows.length) {
        tbody.innerHTML =
            '<tr><td colspan="6" style="text-align: center;">No calls analysed yet</td></tr>';
        return;
    }

    tbody.innerHTML = rows
        .map(row => {
            const a = row.analyses ?? {};
            const when = row.analyzed_at ? new Date(row.analyzed_at).toLocaleString() : 'N/A';
            const sentiment = a.sentiment?.overall_sentiment ?? '—';
            // Only negative is worth colouring: it is the one an operator is looking for.
            const sentimentCell =
                sentiment === 'negative' ? `<strong>${sentiment}</strong>` : sentiment;
            const quality =
                a.quality?.overall_score === undefined
                    ? '—'
                    : Math.round(a.quality.overall_score).toString();
            const keywords = (a.keywords?.keywords ?? []).slice(0, 4).join(', ');
            const summary = a.summary?.summary ?? '';

            return `
                <tr>
                    <td><small>${when}</small></td>
                    <td><small>${escapeHtml(String(row.recording_id ?? ''))}</small></td>
                    <td>${sentimentCell}</td>
                    <td>${quality}</td>
                    <td><small>${escapeHtml(keywords)}</small></td>
                    <td><small>${escapeHtml(summary.slice(0, 160))}</small></td>
                </tr>
            `;
        })
        .join('');
}

function summarise(rows: AnalysisRow[], stats: AnalyticsStats | null): void {
    const negative = rows.filter(
        r => r.analyses?.sentiment?.overall_sentiment === 'negative'
    ).length;
    const scored = rows
        .map(r => r.analyses?.quality?.overall_score)
        .filter((n): n is number => typeof n === 'number');
    const average = scored.length
        ? Math.round(scored.reduce((a, b) => a + b, 0) / scored.length)
        : null;

    if (el('analytics-total')) el('analytics-total')!.textContent = String(rows.length);
    if (el('analytics-negative')) el('analytics-negative')!.textContent = String(negative);
    if (el('analytics-avg-quality')) {
        el('analytics-avg-quality')!.textContent = average === null ? '—' : String(average);
    }

    const banner = el('analytics-banner');
    if (!banner) return;

    if (stats?.auto_analyze === false) {
        banner.className = 'alert-box warning';
        banner.textContent =
            'Automatic analysis is OFF, so this list only shows calls analysed by hand. '
            + 'Set recording.analytics.auto_analyze in config.yml to analyse every call.';
    } else {
        banner.className = 'alert-box';
        banner.textContent =
            'Every recording is analysed once its transcript lands. Analysis reads the '
            + 'transcript only — it never opens the audio.';
    }
}

export async function loadRecordingAnalytics(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const [analysesRes, statsRes] = await Promise.all([
            fetchWithTimeout(`${API_BASE}/api/framework/recording-analytics/analyses`, {
                headers: getAuthHeaders()
            }),
            fetchWithTimeout(`${API_BASE}/api/framework/recording-analytics/statistics`, {
                headers: getAuthHeaders()
            })
        ]);

        const [data, stats]: [AnalysesResponse, AnalyticsStats] = await Promise.all([
            analysesRes.json(),
            statsRes.json()
        ]);

        const rows = data?.analyses ?? [];
        render(rows);
        summarise(rows, stats ?? null);
    } catch (error: unknown) {
        console.error('Error loading recording analytics:', error);
        showNotification('Error loading recording analytics', 'error');
    }
}

export async function searchRecordingAnalytics(): Promise<void> {
    const sentiment = (el('analytics-search-sentiment') as HTMLSelectElement | null)?.value ?? '';
    const keyword = (el('analytics-search-keyword') as HTMLInputElement | null)?.value.trim() ?? '';
    const minQuality =
        (el('analytics-search-quality') as HTMLInputElement | null)?.value.trim() ?? '';

    const criteria: Record<string, unknown> = {};
    if (sentiment) criteria.sentiment = sentiment;
    if (keyword) criteria.keywords = [keyword];
    if (minQuality) criteria.min_quality_score = Number(minQuality);

    if (!Object.keys(criteria).length) {
        await loadRecordingAnalytics();
        return;
    }

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(
            `${API_BASE}/api/framework/recording-analytics/search`,
            { method: 'POST', headers: getAuthHeaders(), body: JSON.stringify(criteria) }
        );
        const data = await response.json();

        // Search returns matching recording ids; the rows themselves come from the list.
        const matches: string[] = (data?.recording_ids ?? data?.results ?? []).map(String);
        const listRes = await fetchWithTimeout(
            `${API_BASE}/api/framework/recording-analytics/analyses`,
            { headers: getAuthHeaders() }
        );
        const list: AnalysesResponse = await listRes.json();
        const rows = (list?.analyses ?? []).filter(r =>
            matches.includes(String(r.recording_id))
        );

        render(rows);
        showNotification(`${rows.length} call(s) matched`, 'success');
    } catch (error: unknown) {
        console.error('Error searching recording analytics:', error);
        showNotification('Error searching recording analytics', 'error');
    }
}

(window as any).loadRecordingAnalytics = loadRecordingAnalytics;
(window as any).searchRecordingAnalytics = searchRecordingAnalytics;
