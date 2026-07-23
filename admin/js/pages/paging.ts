/**
 * Paging page module.
 * Handles paging zones, DAC devices, active pages, and test pages.
 *
 * Field names here mirror the PagingSystem config exactly (zones:
 * extension/name/description/dac_device; devices: device_id/device_type/
 * sip_uri/ip_address/port) so what the form posts is what the backend stores.
 */

import { getAuthHeaders, getApiBaseUrl } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
import { escapeHtml, formatDate } from '../utils/html.ts';

interface PagingZone {
    extension: string;
    name?: string;
    description?: string;
    dac_device?: string | null;
}

interface PagingDevice {
    device_id: string;
    name?: string;
    device_type?: string;
    sip_uri?: string | null;
    ip_address?: string | null;
    port?: number;
    configured_at?: string;
}

interface ActivePage {
    page_id: string;
    from_extension?: string;
    zone_names?: string;
    started_at?: string;
    status?: string;
}

interface PagingStatus {
    enabled: boolean;
    all_call_extension?: string;
}

interface ZonesResponse { zones?: PagingZone[] }
interface DevicesResponse { devices?: PagingDevice[] }
interface ActivePagesResponse { active_pages?: ActivePage[] }
interface ApiResponse { success?: boolean; message?: string; error?: string }

/** Cached so the zone form can offer devices without a second round trip. */
let knownDevices: PagingDevice[] = [];

/**
 * The all-call extension, cached from /status. It is not itself a zone, so it
 * never appears in the zones list -- without this the test-page picker could
 * not reach it at all.
 */
let allCallExtension = '';

/**
 * Pull the human-readable failure out of an API response.
 * The backend reports failures under `error`; only successes carry `message`.
 */
async function errorText(response: Response, fallback: string): Promise<string> {
    try {
        const data: ApiResponse = await response.json();
        return data.error ?? data.message ?? fallback;
    } catch {
        return fallback;
    }
}

export async function loadPagingData(): Promise<void> {
    // Status first: it decides whether the rest of the tab is actionable.
    await loadPagingStatus();
    await Promise.all([loadPagingZones(), loadPagingDevices(), loadActivePages()]);
}

/**
 * Reflect whether the feature is enabled at all. Without this the tab cannot
 * distinguish "paging is off" from "paging is on but empty" -- both return
 * empty collections -- and would offer buttons that can only fail.
 */
export async function loadPagingStatus(): Promise<void> {
    const banner = document.getElementById('paging-disabled-banner') as HTMLElement | null;
    const controls = [
        'add-paging-zone-btn',
        'add-paging-device-btn',
        'test-page-submit'
    ].map(id => document.getElementById(id) as HTMLButtonElement | null);

    try {
        const response = await fetch(`${getApiBaseUrl()}/api/paging/status`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const status: PagingStatus = await response.json();

        if (banner) banner.style.display = status.enabled ? 'none' : 'block';
        controls.forEach(btn => {
            if (btn) btn.disabled = !status.enabled;
        });

        if (status.all_call_extension) {
            allCallExtension = status.all_call_extension;
            const allCall = document.getElementById('paging-all-call-ext');
            if (allCall) allCall.textContent = status.all_call_extension;
        }
    } catch (error: unknown) {
        console.error('Error loading paging status:', error);
    }
}

export async function loadPagingZones(): Promise<void> {
    const tbody = document.getElementById('paging-zones-table-body') as HTMLElement | null;
    if (!tbody) return;

    try {
        const response = await fetch(`${getApiBaseUrl()}/api/paging/zones`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: ZonesResponse = await response.json();
        const zones = data.zones ?? [];

        if (zones.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5">No paging zones configured</td></tr>';
        } else {
            tbody.innerHTML = zones.map(z => `
                <tr>
                    <td>${escapeHtml(z.extension ?? '')}</td>
                    <td>${escapeHtml(z.name ?? '')}</td>
                    <td>${escapeHtml(z.description ?? '')}</td>
                    <td>${z.dac_device
                        ? escapeHtml(z.dac_device)
                        : '<em>none — answers but plays no audio</em>'}</td>
                    <td><button class="btn btn-danger btn-sm"
                        onclick="deletePagingZone('${escapeHtml(z.extension)}')">Delete</button></td>
                </tr>
            `).join('');
        }

        populateZoneSelect(zones);
    } catch (error: unknown) {
        console.error('Error loading paging zones:', error);
        tbody.innerHTML = '<tr><td colspan="5">Failed to load paging zones</td></tr>';
    }
}

export async function loadPagingDevices(): Promise<void> {
    const tbody = document.getElementById('paging-devices-table-body') as HTMLElement | null;
    if (!tbody) return;

    try {
        const response = await fetch(`${getApiBaseUrl()}/api/paging/devices`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: DevicesResponse = await response.json();
        const devices = data.devices ?? [];
        knownDevices = devices;

        if (devices.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6">No DAC devices configured</td></tr>';
        } else {
            tbody.innerHTML = devices.map(d => {
                const address = d.sip_uri
                    ?? (d.ip_address ? `${d.ip_address}:${d.port ?? 5060}` : '');
                return `
                <tr>
                    <td>${escapeHtml(d.device_id)}</td>
                    <td>${escapeHtml(d.name ?? d.device_id)}</td>
                    <td>${escapeHtml(d.device_type ?? '')}</td>
                    <td>${escapeHtml(address)}</td>
                    <td>${escapeHtml(d.configured_at ? formatDate(d.configured_at) : '')}</td>
                    <td><button class="btn btn-danger btn-sm"
                        onclick="deletePagingDevice('${escapeHtml(d.device_id)}')">Delete</button></td>
                </tr>`;
            }).join('');
        }

        populateDeviceSelect(devices);
    } catch (error: unknown) {
        console.error('Error loading paging devices:', error);
        tbody.innerHTML = '<tr><td colspan="6">Failed to load DAC devices</td></tr>';
    }
}

export async function loadActivePages(): Promise<void> {
    const tbody = document.getElementById('active-pages-table-body') as HTMLElement | null;
    if (!tbody) return;

    try {
        const response = await fetch(`${getApiBaseUrl()}/api/paging/active`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: ActivePagesResponse = await response.json();
        const pages = data.active_pages ?? [];

        tbody.innerHTML = pages.length === 0
            ? '<tr><td colspan="5">No active paging sessions</td></tr>'
            : pages.map(p => `
                <tr>
                    <td>${escapeHtml(p.page_id)}</td>
                    <td>${escapeHtml(p.from_extension ?? '')}</td>
                    <td>${escapeHtml(p.zone_names ?? '')}</td>
                    <td>${escapeHtml(p.started_at ? formatDate(p.started_at) : '')}</td>
                    <td>${escapeHtml(p.status ?? '')}</td>
                </tr>
            `).join('');
    } catch (error: unknown) {
        console.error('Error loading active pages:', error);
        tbody.innerHTML = '<tr><td colspan="5">Failed to load active pages</td></tr>';
    }
}

/** Fill the test-page zone picker from the configured zones. */
function populateZoneSelect(zones: PagingZone[]): void {
    const select = document.getElementById('test-page-zone') as HTMLSelectElement | null;
    if (!select) return;

    const previous = select.value;
    const allCallOption = allCallExtension
        ? `<option value="${escapeHtml(allCallExtension)}">${escapeHtml(allCallExtension)} — All Zones</option>`
        : '';

    select.innerHTML = '<option value="">Select Zone</option>'
        + allCallOption
        + zones.map(z =>
            `<option value="${escapeHtml(z.extension)}">${escapeHtml(z.extension)} — ${escapeHtml(z.name ?? '')}</option>`
        ).join('');
    if (previous) select.value = previous;
}

/** Fill the add-zone form's device picker from the configured devices. */
function populateDeviceSelect(devices: PagingDevice[]): void {
    const select = document.getElementById('paging-zone-dac-device') as HTMLSelectElement | null;
    if (!select) return;

    select.innerHTML = '<option value="">None (zone answers but plays no audio)</option>'
        + devices.map(d =>
            `<option value="${escapeHtml(d.device_id)}">${escapeHtml(d.name ?? d.device_id)}</option>`
        ).join('');
}

export function showAddPagingZoneModal(): void {
    // Refresh the device picker in case devices changed since the last load.
    populateDeviceSelect(knownDevices);
    const modal = document.getElementById('add-paging-zone-modal') as HTMLElement | null;
    if (modal) modal.classList.add('active');
}

export function closeAddPagingZoneModal(): void {
    const modal = document.getElementById('add-paging-zone-modal') as HTMLElement | null;
    if (modal) modal.classList.remove('active');
    (document.getElementById('add-paging-zone-form') as HTMLFormElement | null)?.reset();
}

export function showAddPagingDeviceModal(): void {
    const modal = document.getElementById('add-paging-device-modal') as HTMLElement | null;
    if (modal) modal.classList.add('active');
}

export function closeAddPagingDeviceModal(): void {
    const modal = document.getElementById('add-paging-device-modal') as HTMLElement | null;
    if (modal) modal.classList.remove('active');
    (document.getElementById('add-paging-device-form') as HTMLFormElement | null)?.reset();
}

const val = (id: string): string =>
    ((document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null)?.value ?? '').trim();

export async function addPagingZone(event: Event): Promise<void> {
    event.preventDefault();

    const zone = {
        extension: val('paging-zone-extension'),
        name: val('paging-zone-name'),
        description: val('paging-zone-description'),
        dac_device: val('paging-zone-dac-device') || null
    };

    try {
        const response = await fetch(`${getApiBaseUrl()}/api/paging/zones`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify(zone)
        });

        if (response.ok) {
            showNotification(`Zone ${zone.name} added`, 'success');
            closeAddPagingZoneModal();
            loadPagingZones();
        } else {
            showNotification(await errorText(response, 'Failed to add zone'), 'error');
        }
    } catch (error: unknown) {
        console.error('Error adding paging zone:', error);
        showNotification('Failed to add zone', 'error');
    }
}

export async function addPagingDevice(event: Event): Promise<void> {
    event.preventDefault();

    const portValue = val('paging-device-port');
    const device = {
        device_id: val('paging-device-id'),
        name: val('paging-device-name') || undefined,
        device_type: val('paging-device-type'),
        ip_address: val('paging-device-ip'),
        sip_uri: val('paging-device-sip-uri'),
        port: portValue ? Number(portValue) : 5060
    };

    try {
        const response = await fetch(`${getApiBaseUrl()}/api/paging/devices`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify(device)
        });

        if (response.ok) {
            showNotification(`Device ${device.device_id} added`, 'success');
            closeAddPagingDeviceModal();
            loadPagingDevices();
        } else {
            showNotification(await errorText(response, 'Failed to add device'), 'error');
        }
    } catch (error: unknown) {
        console.error('Error adding paging device:', error);
        showNotification('Failed to add device', 'error');
    }
}

export async function deletePagingZone(extension: string): Promise<void> {
    if (!confirm(`Delete paging zone ${extension}?`)) return;

    try {
        const response = await fetch(`${getApiBaseUrl()}/api/paging/zones/${encodeURIComponent(extension)}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (response.ok) {
            showNotification(`Zone ${extension} deleted`, 'success');
            loadPagingZones();
        } else {
            showNotification(await errorText(response, 'Failed to delete zone'), 'error');
        }
    } catch (error: unknown) {
        console.error('Error deleting paging zone:', error);
        showNotification('Failed to delete zone', 'error');
    }
}

export async function deletePagingDevice(deviceId: string): Promise<void> {
    if (!confirm(`Delete paging device ${deviceId}?`)) return;

    try {
        const response = await fetch(`${getApiBaseUrl()}/api/paging/devices/${encodeURIComponent(deviceId)}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });

        if (response.ok) {
            showNotification(`Device ${deviceId} deleted`, 'success');
            // Zones referencing the device were unlinked server-side, so
            // reload them too rather than leaving a stale device name shown.
            loadPagingDevices();
            loadPagingZones();
        } else {
            showNotification(await errorText(response, 'Failed to delete device'), 'error');
        }
    } catch (error: unknown) {
        console.error('Error deleting paging device:', error);
        showNotification('Failed to delete device', 'error');
    }
}

export async function startTestPage(event: Event): Promise<void> {
    event.preventDefault();

    const body = {
        from_extension: val('test-page-from'),
        zone: val('test-page-zone')
    };

    try {
        const response = await fetch(`${getApiBaseUrl()}/api/paging/test`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });

        if (response.ok) {
            // 202: the extension is ringing; the page opens when it answers.
            showNotification(
                `Ringing ${body.from_extension} — answer to page zone ${body.zone}`,
                'success'
            );
            loadActivePages();
        } else {
            showNotification(await errorText(response, 'Failed to start test page'), 'error');
        }
    } catch (error: unknown) {
        console.error('Error starting test page:', error);
        showNotification('Failed to start test page', 'error');
    }
}

// Backward compatibility
window.loadPagingData = loadPagingData;
window.loadPagingZones = loadPagingZones;
window.loadPagingDevices = loadPagingDevices;
window.loadActivePages = loadActivePages;
window.deletePagingZone = deletePagingZone;
window.deletePagingDevice = deletePagingDevice;
/* eslint-disable-next-line @typescript-eslint/no-explicit-any -- legacy backward compat */
(window as any).loadPagingStatus = loadPagingStatus;
(window as any).showAddPagingZoneModal = showAddPagingZoneModal;
(window as any).closeAddPagingZoneModal = closeAddPagingZoneModal;
(window as any).showAddPagingDeviceModal = showAddPagingDeviceModal;
(window as any).closeAddPagingDeviceModal = closeAddPagingDeviceModal;
(window as any).addPagingZone = addPagingZone;
(window as any).addPagingDevice = addPagingDevice;
(window as any).startTestPage = startTestPage;
