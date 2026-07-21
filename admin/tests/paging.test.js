/**
 * @jest-environment jsdom
 */

// Mock dependency modules before importing the module under test.
// jest.mock() calls are hoisted above require() by the CJS transform,
// ensuring mocks are registered before paging.ts loads its imports.
jest.mock('../js/api/client.ts', () => ({
  getAuthHeaders: jest.fn(() => ({
    'Content-Type': 'application/json',
    'Authorization': 'Bearer test-token'
  })),
  getApiBaseUrl: jest.fn(() => 'http://localhost:9000')
}));

jest.mock('../js/ui/notifications.ts', () => ({
  showNotification: jest.fn()
}));

import { describe, it, expect, beforeEach } from '@jest/globals';
import {
  loadPagingStatus,
  loadPagingZones,
  loadPagingDevices,
  loadActivePages,
  addPagingZone,
  addPagingDevice,
  deletePagingZone,
  deletePagingDevice,
  startTestPage
} from '../js/pages/paging.ts';
import { showNotification } from '../js/ui/notifications.ts';

global.fetch = jest.fn();

/** Build a Response-like object; `body` is returned from .json(). */
function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body };
}

const DOM = `
  <div id="paging-disabled-banner" style="display: none;"></div>
  <span id="paging-all-call-ext">700</span>
  <button id="add-paging-zone-btn"></button>
  <button id="add-paging-device-btn"></button>
  <button id="test-page-submit"></button>
  <table><tbody id="paging-zones-table-body"></tbody></table>
  <table><tbody id="paging-devices-table-body"></tbody></table>
  <table><tbody id="active-pages-table-body"></tbody></table>
  <input id="test-page-from" />
  <select id="test-page-zone"></select>
  <select id="paging-zone-dac-device"></select>
  <form id="add-paging-zone-form">
    <input id="paging-zone-extension" />
    <input id="paging-zone-name" />
    <input id="paging-zone-description" />
  </form>
  <form id="add-paging-device-form">
    <input id="paging-device-id" />
    <input id="paging-device-name" />
    <select id="paging-device-type"><option value="sip_gateway">sip</option></select>
    <input id="paging-device-ip" />
    <input id="paging-device-port" />
    <input id="paging-device-sip-uri" />
  </form>
  <div id="add-paging-zone-modal" class="modal"></div>
  <div id="add-paging-device-modal" class="modal"></div>
`;

function setValue(id, value) {
  document.getElementById(id).value = value;
}

/** The submit event a form onsubmit handler receives. */
function submitEvent() {
  return { preventDefault: jest.fn() };
}

describe('Paging System', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    document.body.innerHTML = DOM;
    global.confirm = jest.fn(() => true);
  });

  describe('loadPagingStatus', () => {
    it('hides the banner and enables controls when paging is enabled', async () => {
      fetch.mockResolvedValueOnce(
        jsonResponse({ enabled: true, all_call_extension: '750' })
      );

      await loadPagingStatus();

      expect(document.getElementById('paging-disabled-banner').style.display).toBe('none');
      expect(document.getElementById('add-paging-zone-btn').disabled).toBe(false);
      expect(document.getElementById('paging-all-call-ext').textContent).toBe('750');
    });

    it('shows the banner and disables every write control when paging is off', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({ enabled: false }));

      await loadPagingStatus();

      expect(document.getElementById('paging-disabled-banner').style.display).toBe('block');
      // Disabled means "off", not "empty" -- the buttons must not invite a click
      // that can only 503.
      expect(document.getElementById('add-paging-zone-btn').disabled).toBe(true);
      expect(document.getElementById('add-paging-device-btn').disabled).toBe(true);
      expect(document.getElementById('test-page-submit').disabled).toBe(true);
    });
  });

  describe('loadPagingZones', () => {
    it('renders zones from the wrapped response', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({
        zones: [
          { extension: '701', name: 'Warehouse', description: 'Back racks', dac_device: 'dac-1' }
        ]
      }));

      await loadPagingZones();

      const html = document.getElementById('paging-zones-table-body').innerHTML;
      expect(html).toContain('701');
      expect(html).toContain('Warehouse');
      expect(html).toContain('Back racks');
      expect(html).toContain('dac-1');
    });

    it('flags a zone with no device rather than showing a blank cell', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({
        zones: [{ extension: '701', name: 'Warehouse', dac_device: null }]
      }));

      await loadPagingZones();

      expect(document.getElementById('paging-zones-table-body').innerHTML)
        .toContain('answers but plays no audio');
    });

    it('populates the test-page zone picker', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({
        zones: [
          { extension: '701', name: 'Warehouse' },
          { extension: '702', name: 'Office' }
        ]
      }));

      await loadPagingZones();

      const options = [...document.getElementById('test-page-zone').querySelectorAll('option')];
      expect(options.map(o => o.value)).toEqual(expect.arrayContaining(['701', '702']));
    });

    it('offers the all-call extension, which is not itself a zone', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({ enabled: true, all_call_extension: '700' }));
      await loadPagingStatus();

      fetch.mockResolvedValueOnce(jsonResponse({
        zones: [{ extension: '701', name: 'Warehouse' }]
      }));
      await loadPagingZones();

      const options = [...document.getElementById('test-page-zone').querySelectorAll('option')];
      const allCall = options.find(o => o.value === '700');
      expect(allCall).toBeDefined();
      expect(allCall.textContent).toContain('All Zones');
    });

    it('shows an empty state when there are no zones', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({ zones: [] }));

      await loadPagingZones();

      expect(document.getElementById('paging-zones-table-body').innerHTML)
        .toContain('No paging zones configured');
    });

    it('escapes zone values into the table', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({
        zones: [{ extension: '701', name: '<img src=x onerror=alert(1)>' }]
      }));

      await loadPagingZones();

      const html = document.getElementById('paging-zones-table-body').innerHTML;
      expect(html).not.toContain('<img');
      expect(html).toContain('&lt;img');
    });
  });

  describe('loadPagingDevices', () => {
    it('renders devices from the wrapped response', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({
        devices: [{
          device_id: 'dac-1',
          name: 'Main PA',
          device_type: 'sip_gateway',
          sip_uri: 'sip:paging@192.168.1.10'
        }]
      }));

      await loadPagingDevices();

      const html = document.getElementById('paging-devices-table-body').innerHTML;
      expect(html).toContain('dac-1');
      expect(html).toContain('Main PA');
      expect(html).toContain('sip:paging@192.168.1.10');
    });

    it('falls back to ip:port when no SIP URI is set', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({
        devices: [{ device_id: 'dac-1', ip_address: '192.168.1.10', port: 5062 }]
      }));

      await loadPagingDevices();

      expect(document.getElementById('paging-devices-table-body').innerHTML)
        .toContain('192.168.1.10:5062');
    });

    it('populates the zone form device picker', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({
        devices: [{ device_id: 'dac-1', name: 'Main PA' }]
      }));

      await loadPagingDevices();

      const options = document.getElementById('paging-zone-dac-device').querySelectorAll('option');
      expect(options).toHaveLength(2);
      expect(options[1].value).toBe('dac-1');
    });
  });

  describe('loadActivePages', () => {
    it('reads the active_pages key the API actually returns', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({
        active_pages: [{
          page_id: 'page-1',
          from_extension: '1001',
          zone_names: 'Warehouse',
          status: 'active'
        }]
      }));

      await loadActivePages();

      const html = document.getElementById('active-pages-table-body').innerHTML;
      expect(html).toContain('page-1');
      expect(html).toContain('1001');
      expect(html).toContain('Warehouse');
    });

    it('shows an empty state when nothing is paging', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({ active_pages: [] }));

      await loadActivePages();

      expect(document.getElementById('active-pages-table-body').innerHTML)
        .toContain('No active paging sessions');
    });
  });

  describe('addPagingZone', () => {
    it('posts the field names the backend expects', async () => {
      setValue('paging-zone-extension', '701');
      setValue('paging-zone-name', 'Warehouse');
      setValue('paging-zone-description', 'Back racks');
      setValue('paging-zone-dac-device', '');

      fetch
        .mockResolvedValueOnce(jsonResponse({ success: true }))
        .mockResolvedValueOnce(jsonResponse({ zones: [] }));

      await addPagingZone(submitEvent());

      const body = JSON.parse(fetch.mock.calls[0][1].body);
      expect(body).toEqual({
        extension: '701',
        name: 'Warehouse',
        description: 'Back racks',
        dac_device: null
      });
      expect(showNotification).toHaveBeenCalledWith(expect.stringContaining('Warehouse'), 'success');
    });

    it('surfaces the API error message instead of a generic failure', async () => {
      setValue('paging-zone-extension', '701');
      setValue('paging-zone-name', 'Warehouse');

      fetch.mockResolvedValueOnce(
        jsonResponse({ error: 'Paging zone 701 already exists' }, { ok: false, status: 409 })
      );

      await addPagingZone(submitEvent());

      expect(showNotification).toHaveBeenCalledWith('Paging zone 701 already exists', 'error');
    });

    it('reports a reason when paging is disabled', async () => {
      setValue('paging-zone-extension', '701');
      setValue('paging-zone-name', 'Warehouse');

      fetch.mockResolvedValueOnce(
        jsonResponse({ error: 'Paging system is not enabled' }, { ok: false, status: 503 })
      );

      await addPagingZone(submitEvent());

      expect(showNotification).toHaveBeenCalledWith('Paging system is not enabled', 'error');
    });
  });

  describe('addPagingDevice', () => {
    it('posts device_type/sip_uri/ip_address as the backend names them', async () => {
      setValue('paging-device-id', 'dac-1');
      setValue('paging-device-name', 'Main PA');
      setValue('paging-device-ip', '192.168.1.10');
      setValue('paging-device-port', '5062');
      setValue('paging-device-sip-uri', 'sip:paging@192.168.1.10');

      fetch
        .mockResolvedValueOnce(jsonResponse({ success: true }))
        .mockResolvedValueOnce(jsonResponse({ devices: [] }));

      await addPagingDevice(submitEvent());

      const body = JSON.parse(fetch.mock.calls[0][1].body);
      expect(body.device_id).toBe('dac-1');
      expect(body.device_type).toBe('sip_gateway');
      expect(body.ip_address).toBe('192.168.1.10');
      expect(body.sip_uri).toBe('sip:paging@192.168.1.10');
      // Numeric, not the string the input yields.
      expect(body.port).toBe(5062);
    });

    it('defaults the port when the field is left blank', async () => {
      setValue('paging-device-id', 'dac-1');
      setValue('paging-device-ip', '192.168.1.10');
      setValue('paging-device-sip-uri', 'sip:paging@192.168.1.10');
      setValue('paging-device-port', '');

      fetch
        .mockResolvedValueOnce(jsonResponse({ success: true }))
        .mockResolvedValueOnce(jsonResponse({ devices: [] }));

      await addPagingDevice(submitEvent());

      expect(JSON.parse(fetch.mock.calls[0][1].body).port).toBe(5060);
    });
  });

  describe('deletePagingZone', () => {
    it('notifies on a failed delete rather than failing silently', async () => {
      fetch.mockResolvedValueOnce(
        jsonResponse({ error: 'Paging zone 701 not found' }, { ok: false, status: 404 })
      );

      await deletePagingZone('701');

      expect(showNotification).toHaveBeenCalledWith('Paging zone 701 not found', 'error');
    });

    it('does nothing when the confirm is declined', async () => {
      global.confirm = jest.fn(() => false);

      await deletePagingZone('701');

      expect(fetch).not.toHaveBeenCalled();
    });
  });

  describe('deletePagingDevice', () => {
    it('reloads zones too, since the server unlinks them', async () => {
      fetch
        .mockResolvedValueOnce(jsonResponse({ success: true }))
        .mockResolvedValueOnce(jsonResponse({ devices: [] }))
        .mockResolvedValueOnce(jsonResponse({ zones: [] }));

      await deletePagingDevice('dac-1');

      const urls = fetch.mock.calls.map(c => c[0]);
      expect(urls[0]).toContain('/api/paging/devices/dac-1');
      expect(urls).toContain('http://localhost:9000/api/paging/zones');
    });
  });

  describe('startTestPage', () => {
    it('posts the announcing extension and zone', async () => {
      setValue('test-page-from', '1001');
      document.getElementById('test-page-zone').innerHTML = '<option value="701">701</option>';
      document.getElementById('test-page-zone').value = '701';

      fetch
        .mockResolvedValueOnce(jsonResponse({ success: true, call_id: 'c1' }, { status: 202 }))
        .mockResolvedValueOnce(jsonResponse({ active_pages: [] }));

      await startTestPage(submitEvent());

      const [url, options] = fetch.mock.calls[0];
      expect(url).toContain('/api/paging/test');
      expect(JSON.parse(options.body)).toEqual({ from_extension: '1001', zone: '701' });
      expect(showNotification).toHaveBeenCalledWith(
        expect.stringContaining('Ringing 1001'),
        'success'
      );
    });

    it('surfaces a rejected test page', async () => {
      setValue('test-page-from', '1001');
      document.getElementById('test-page-zone').innerHTML = '<option value="999">999</option>';
      document.getElementById('test-page-zone').value = '999';

      fetch.mockResolvedValueOnce(
        jsonResponse({ error: '999 is not a paging extension' }, { ok: false, status: 400 })
      );

      await startTestPage(submitEvent());

      expect(showNotification).toHaveBeenCalledWith('999 is not a paging extension', 'error');
    });
  });
});
