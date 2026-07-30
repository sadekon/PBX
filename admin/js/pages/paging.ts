/**
 * Paging page module.
 * Handles paging zones, devices, and active pages.
 */

import { getAuthHeaders, getApiBaseUrl } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
import { escapeHtml } from '../utils/html.ts';

interface PagingZone {
    id: string;
    name?: string;
    number?: string;
    devices?: string[];
}

interface PagingZonesResponse {
    zones?: PagingZone[];
}

interface PagingDevice {
    id: string;
    name?: string;
    type?: string;
    sip_address?: string;
    status?: string;
    device_id?: string;
}

interface ApiResponse {
    success?: boolean;
    message?: string;
}

interface PagingDevicesResponse {
    devices?: PagingDevice[];
}

interface ActivePage {
    zone: string;
    initiator: string;
}

interface ActivePagesResponse {
    pages?: ActivePage[];
}

export async function loadPagingData(): Promise<void> {
    await Promise.all([loadPagingZones(), loadPagingDevices(), loadActivePages()]);
}

export async function loadPagingZones(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/paging/zones`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: PagingZonesResponse = await response.json();

        const tbody = document.getElementById('paging-zones-table-body') as HTMLElement | null;
        if (!tbody) return;

        const zones = data.zones ?? [];
        if (zones.length === 0) {
            tbody.innerHTML = '<tr><td colspan="4">No paging zones</td></tr>';
            return;
        }

        tbody.innerHTML = zones.map(z => `
            <tr>
                <td>${escapeHtml(z.name || '')}</td>
                <td>${escapeHtml(z.number || '')}</td>
                <td>${z.devices?.length || 0} devices</td>
                <td><button class="btn btn-danger btn-sm" onclick="deletePagingZone('${z.id}')">Delete</button></td>
            </tr>
        `).join('');
    } catch (error: unknown) {
        console.error('Error loading paging zones:', error);
    }
}

export async function loadPagingDevices(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/paging/devices`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) return;
        const data: PagingDevicesResponse = await response.json();

        const container = document.getElementById('paging-devices-table-body') as HTMLElement | null;
        if (container) {
            const devices = data.devices ?? [];
            container.innerHTML = devices.length === 0
                ? '<div class="info-box">No paging devices</div>'
                : devices.map(d => `<div class="device-item">${escapeHtml(d.name || d.id)}</div>`).join('');
        }
    } catch (error: unknown) {
        console.error('Error loading paging devices:', error);
    }
}

export async function loadActivePages(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/paging/active`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) return;
        const data: ActivePagesResponse = await response.json();

        const container = document.getElementById('active-pages-table-body') as HTMLElement | null;
        if (container) {
            const pages = data.pages ?? [];
            container.innerHTML = pages.length === 0
                ? '<div class="info-box">No active pages</div>'
                : pages.map(p => `<div class="page-item">${escapeHtml(p.zone)} - ${escapeHtml(p.initiator)}</div>`).join('');
        }
    } catch (error: unknown) {
        console.error('Error loading active pages:', error);
    }
}

export async function deletePagingZone(zoneId: string): Promise<void> {
    if (!confirm('Delete this paging zone?')) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/paging/zones/${zoneId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });
        if (response.ok) {
            showNotification('Paging zone deleted', 'success');
            loadPagingZones();
        }
    } catch (error: unknown) {
        console.error('Error deleting paging zone:', error);
        showNotification('Failed to delete zone', 'error');
    }
}

export function closeZoneModal(): void {
    document.getElementById('paging-zone-modal')?.remove();
}

export function showAddZoneModal(): void {
    closeZoneModal();
    const modal = `
        <div id="paging-zone-modal" class="modal" style="display: block;">
            <div class="modal-content">
                <div class="modal-header">
                    <h3>➕ Add Paging Zone</h3>
                    <span class="close" onclick="closeZoneModal()">&times;</span>
                </div>
                <form id="paging-zone-form">
                    <div class="form-group">
                        <label for="zone-extension">Zone Extension:</label>
                        <input type="text" id="zone-extension" required placeholder="701">
                        <small>Extension callers dial to page this zone</small>
                    </div>
                    <div class="form-group">
                        <label for="zone-name">Zone Name:</label>
                        <input type="text" id="zone-name" required placeholder="Warehouse">
                    </div>
                    <div class="form-group">
                        <label for="zone-description">Description:</label>
                        <input type="text" id="zone-description" placeholder="Optional">
                    </div>
                    <div class="form-group">
                        <label for="zone-device-id">Device ID:</label>
                        <input type="text" id="zone-device-id" placeholder="Optional">
                        <small>Associate a paging device with this zone</small>
                    </div>
                    <div class="modal-actions">
                        <button type="button" class="btn btn-secondary" onclick="closeZoneModal()">Cancel</button>
                        <button type="submit" class="btn btn-success">Add Zone</button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', modal);
    const form = document.getElementById('paging-zone-form') as HTMLFormElement;
    form.onsubmit = (e: Event) => {
        e.preventDefault();
        void submitAddZone();
    };
}

async function submitAddZone(): Promise<void> {
    const extension = (document.getElementById('zone-extension') as HTMLInputElement).value.trim();
    const name = (document.getElementById('zone-name') as HTMLInputElement).value.trim();
    const description = (document.getElementById('zone-description') as HTMLInputElement).value.trim();
    const deviceId = (document.getElementById('zone-device-id') as HTMLInputElement).value.trim();
    if (!extension || !name) return;

    const zoneData = {
        extension: extension,
        name: name,
        description: description,
        device_id: deviceId
    };

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/paging/zones`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify(zoneData)
        });
        const data: ApiResponse = await response.json();
        if (data.success) {
            showNotification(`Zone ${name} added successfully`, 'success');
            closeZoneModal();
            loadPagingZones();
        } else {
            showNotification(data.message ?? 'Failed to add zone', 'error');
        }
    } catch (error: unknown) {
        console.error(`Error adding zone ${name}:`, error);
        showNotification(`Error adding zone ${name}`, 'error');
    }
}

export function closeDeviceModal(): void {
    document.getElementById('paging-device-modal')?.remove();
}

export function showAddDeviceModal(): void {
    closeDeviceModal();
    const modal = `
        <div id="paging-device-modal" class="modal" style="display: block;">
            <div class="modal-content">
                <div class="modal-header">
                    <h3>➕ Add Paging Device</h3>
                    <span class="close" onclick="closeDeviceModal()">&times;</span>
                </div>
                <form id="paging-device-form">
                    <div class="form-group">
                        <label for="device-id">Device ID:</label>
                        <input type="text" id="device-id" required placeholder="dac-1">
                        <small>Unique identifier for this device</small>
                    </div>
                    <div class="form-group">
                        <label for="device-name">Device Name:</label>
                        <input type="text" id="device-name" required placeholder="Main PA System">
                    </div>
                    <div class="form-group">
                        <label for="device-type">Device Type:</label>
                        <input type="text" id="device-type" value="sip_gateway" placeholder="sip_gateway">
                    </div>
                    <div class="form-group">
                        <label for="device-sip-address">SIP Address:</label>
                        <input type="text" id="device-sip-address" placeholder="paging@192.168.1.10:5060">
                    </div>
                    <div class="modal-actions">
                        <button type="button" class="btn btn-secondary" onclick="closeDeviceModal()">Cancel</button>
                        <button type="submit" class="btn btn-success">Add Device</button>
                    </div>
                </form>
            </div>
        </div>
    `;
    document.body.insertAdjacentHTML('beforeend', modal);
    const form = document.getElementById('paging-device-form') as HTMLFormElement;
    form.onsubmit = (e: Event) => {
        e.preventDefault();
        void submitAddDevice();
    };
}

async function submitAddDevice(): Promise<void> {
    const deviceId = (document.getElementById('device-id') as HTMLInputElement).value.trim();
    const name = (document.getElementById('device-name') as HTMLInputElement).value.trim();
    const type = (document.getElementById('device-type') as HTMLInputElement).value.trim() || 'sip_gateway';
    const sipAddress = (document.getElementById('device-sip-address') as HTMLInputElement).value.trim();
    if (!deviceId || !name) return;

    const deviceData = {
        device_id: deviceId,
        name: name,
        type: type,
        sip_address: sipAddress
    };

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/paging/devices`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify(deviceData)
        });
        const data: ApiResponse = await response.json();
        if (data.success) {
            showNotification(`Device ${name} added successfully`, 'success');
            closeDeviceModal();
            loadPagingDevices();
        } else {
            showNotification(data.message ?? 'Failed to add device', 'error');
        }
    } catch (error: unknown) {
        console.error(`Error adding device ${name}:`, error);
        showNotification(`Error adding device ${name}`, 'error');
    }
}

export async function deletePagingDevice(deviceId: string): Promise<void> {
    if (!confirm(`Delete paging device ${deviceId}?`)) return;

    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/paging/devices/${deviceId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });
        const data: ApiResponse = await response.json();
        if (data.success) {
            showNotification(`Device ${deviceId} deleted`, 'success');
            loadPagingDevices();
        } else {
            showNotification(data.message ?? 'Failed to delete device', 'error');
        }
    } catch (error: unknown) {
        console.error('Error deleting device:', error);
        showNotification('Error deleting device', 'error');
    }
}

// Backward compatibility
window.loadPagingData = loadPagingData;
window.loadPagingZones = loadPagingZones;
window.loadPagingDevices = loadPagingDevices;
window.loadActivePages = loadActivePages;
window.deletePagingZone = deletePagingZone;
window.showAddZoneModal = showAddZoneModal;
window.closeZoneModal = closeZoneModal;
window.showAddDeviceModal = showAddDeviceModal;
window.closeDeviceModal = closeDeviceModal;
window.deletePagingDevice = deletePagingDevice;
