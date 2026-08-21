/**
 * @jest-environment jsdom
 *
 * This page tells an operator what will happen when someone dials a zone, and most of what
 * it reports is not visible anywhere else until a page has already gone wrong: a zone that
 * reaches nothing, a destination whose circuit nobody selects, a vendor guess that decides
 * whether an amplifier picks up at all.
 *
 * The labels are the interesting part. "Caller dials" and a blank cell would look the same
 * on screen and mean opposite things -- one is the normal arrangement inherited from the
 * analogue system, the other would be a destination that silently defaults. Likewise
 * "paging is off", "nothing matched the filter", and "no zones exist" all render an empty
 * list and each needs a different fix. These tests hold those apart.
 */

jest.mock('../js/api/client.ts', () => ({
  getAuthHeaders: jest.fn(() => ({ Authorization: 'Bearer test-token' })),
  getApiBaseUrl: jest.fn(() => 'http://localhost:9000'),
  fetchWithTimeout: jest.fn((url, options) => global.fetch(url, options))
}));

jest.mock('../js/ui/notifications.ts', () => ({
  showNotification: jest.fn()
}));

import { describe, it, expect, beforeEach } from '@jest/globals';

let page;
let showNotification;

global.fetch = jest.fn();

function zone(overrides) {
  return {
    id: 1,
    extension: '799',
    name: 'All buildings',
    description: '',
    enabled: true,
    max_duration_seconds: 120,
    destinations: [],
    ...overrides
  };
}

function destination(overrides) {
  return {
    id: 10,
    zone_id: 1,
    kind: 'sip_endpoint',
    label: null,
    enabled: true,
    endpoint_extension: '1501',
    auto_answer_override: null,
    dtmf_sequence: null,
    ...overrides
  };
}

/** Drains pending promises, however many links the handler's await chain has. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function jsonResponse(body, status = 200) {
  return { ok: status < 400, status, json: async () => body };
}

function setupDom() {
  document.body.innerHTML = `
    <input type="search" id="paging-search">
    <input type="checkbox" id="paging-enabled-only">
    <button id="paging-clear-filter" hidden>Clear filters</button>
    <button id="paging-refresh">Refresh</button>
    <button id="paging-add-zone">Add zone</button>
    <span id="paging-count"></span>
    <span id="paging-zone-count"></span>
    <span id="paging-destination-count"></span>
    <span id="paging-active-count"></span>
    <div id="paging-active-section" hidden></div>
    <div id="paging-active-list"></div>
    <div id="paging-list"></div>

    <div id="add-paging-zone-modal" class="modal">
      <h3 id="paging-zone-dialog-title"></h3>
      <p id="paging-zone-dialog-sub"></p>
      <button id="paging-zone-dialog-close"></button>
      <form id="add-paging-zone-form">
        <input type="text" id="paging-zone-extension">
        <span id="paging-zone-extension-note"></span>
        <input type="text" id="paging-zone-name">
        <input type="text" id="paging-zone-description">
        <input type="number" id="paging-zone-max-duration">
        <input type="checkbox" id="paging-zone-enabled" checked>
      </form>
      <button id="paging-zone-dialog-delete"></button>
      <button id="paging-zone-dialog-cancel"></button>
      <button type="submit" form="add-paging-zone-form" id="paging-zone-dialog-save"></button>
    </div>

    <div id="add-paging-destination-modal" class="modal">
      <h3 id="paging-destination-dialog-title"></h3>
      <p id="paging-destination-dialog-sub"></p>
      <button id="paging-destination-dialog-close"></button>
      <form id="add-paging-destination-form">
        <select id="paging-destination-endpoint"></select>
        <span id="paging-destination-endpoint-note"></span>
        <input type="text" id="paging-destination-label">
        <input type="text" id="paging-destination-dtmf">
        <select id="paging-destination-auto-answer">
          <option value="" selected></option>
          <option value="cisco">Cisco</option>
          <option value="none">Send nothing</option>
        </select>
      </form>
      <button id="paging-destination-dialog-cancel"></button>
      <button type="submit" form="add-paging-destination-form" id="paging-destination-dialog-save"></button>
    </div>

    <div id="test-paging-modal" class="modal">
      <h3 id="test-paging-dialog-title"></h3>
      <p id="test-paging-dialog-sub"></p>
      <button id="test-paging-dialog-close"></button>
      <form id="test-paging-form">
        <input type="text" id="test-paging-extension">
      </form>
      <button id="test-paging-dialog-cancel"></button>
      <button type="submit" form="test-paging-form" id="test-paging-dialog-save"></button>
    </div>
  `;
}

describe('Paging page', () => {
  beforeEach(async () => {
    jest.clearAllMocks();
    jest.resetModules();
    setupDom();
    // jsdom has no confirm(); every destructive action goes through one.
    window.confirm = jest.fn(() => true);
    page = await import('../js/pages/paging.ts');
    ({ showNotification } = await import('../js/ui/notifications.ts'));
  });

  /** Routes by URL rather than call order, since the four requests race. */
  function respond({
    zones = [],
    active = [],
    endpoints = [],
    status = { enabled: true, persistent: true, default_max_duration: 120 },
    zonesStatus = 200
  }) {
    fetch.mockImplementation((url) => {
      const target = String(url);
      if (target.includes('/api/paging/status')) return Promise.resolve(jsonResponse(status));
      if (target.includes('/api/paging/active')) {
        return Promise.resolve(jsonResponse({ active_pages: active }));
      }
      if (target.includes('/api/paging/endpoints')) {
        return Promise.resolve(jsonResponse({ endpoints }));
      }
      return Promise.resolve(jsonResponse({ zones }, zonesStatus));
    });
  }

  async function load(zones, options = {}) {
    respond({ zones, ...options });
    await page.loadPagingData();
  }

  const list = () => document.getElementById('paging-list');
  const activeList = () => document.getElementById('paging-active-list');

  /** Opens a zone card and returns its rendered destination rows. */
  function expand(zoneId) {
    const toggle = [...document.querySelectorAll('[data-paging-toggle]')]
      .find((el) => el.getAttribute('data-paging-toggle') === String(zoneId));
    toggle.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    const body = [...document.querySelectorAll('[data-paging-body]')]
      .find((el) => el.getAttribute('data-paging-body') === String(zoneId));
    return [...body.querySelectorAll('.plan-row')].map((row) => {
      const stats = {};
      row.querySelectorAll('.meta-stat').forEach((stat) => {
        stats[stat.querySelector('.k').textContent.trim()] =
          stat.querySelector('.v').textContent.trim();
      });
      return { name: row.querySelector('.plan-main strong').textContent.trim(), ...stats };
    });
  }

  const pillsOn = (zoneId) => {
    const card = [...document.querySelectorAll('[data-paging-toggle]')]
      .find((el) => el.getAttribute('data-paging-toggle') === String(zoneId))
      .closest('.card-shell');
    return [...card.querySelectorAll('.pill')].map((pill) => pill.textContent.trim());
  };

  const bodyOf = (id) => document.getElementById(id);

  it('renders one card per zone', async () => {
    await load([zone(), zone({ id: 2, extension: '798' })]);
    expect(list().querySelectorAll('.card-shell')).toHaveLength(2);
  });

  it('sorts zone numbers numerically, so 99 precedes 100', async () => {
    await load([zone({ id: 1, extension: '100' }), zone({ id: 2, extension: '99' })]);
    const numbers = [...list().querySelectorAll('.card-title strong')].map((el) => el.textContent);
    expect(numbers).toEqual(['99', '100']);
  });

  describe('what a destination row reports', () => {
    it('says the caller dials the circuit when no sequence is stored', async () => {
      await load([zone({ destinations: [destination()] })]);
      expect(expand(1)[0].Circuit).toBe('Caller dials');
    });

    it('shows the digits the PBX will dial when a sequence is stored', async () => {
      await load([zone({ destinations: [destination({ dtmf_sequence: '2' })] })]);
      expect(expand(1)[0].Circuit).toBe('2');
    });

    it('reports auto-answer as derived until it is overridden', async () => {
      await load([zone({ destinations: [destination()] })]);
      expect(expand(1)[0]['Auto-answer']).toBe('From vendor');
    });

    it('names the vendor an override forces, rather than its raw key', async () => {
      await load([zone({ destinations: [destination({ auto_answer_override: 'polycom' })] })]);
      expect(expand(1)[0]['Auto-answer']).toBe('Polycom');
    });

    it('spells out an override that deliberately sends no header', async () => {
      await load([zone({ destinations: [destination({ auto_answer_override: 'none' })] })]);
      expect(expand(1)[0]['Auto-answer']).toBe('Nothing sent');
    });

    it('falls back to the extension when no label was given', async () => {
      await load([zone({ destinations: [destination()] })]);
      expect(expand(1)[0].name).toBe('Extension 1501');
    });

    it('prefers the operator’s label over the extension', async () => {
      await load([zone({ destinations: [destination({ label: 'Building 1 amp' })] })]);
      expect(expand(1)[0].name).toBe('Building 1 amp');
    });

    it('warns that a multicast destination is stored but never streamed to', async () => {
      await load([zone({
        destinations: [destination({
          kind: 'multicast', endpoint_extension: null,
          multicast_address: '239.1.1.1', multicast_port: 5004
        })]
      })]);
      expect(expand(1)[0].name).toBe('239.1.1.1:5004');
      expect(pillsOn(1)).toContain('Not yet streamed');
    });
  });

  describe('what a zone card flags', () => {
    it('says nothing about a zone that is enabled, idle and wired up', async () => {
      await load([zone({ destinations: [destination()] })]);
      expect(pillsOn(1)).toEqual([]);
    });

    it('flags a zone that would answer and reach no one', async () => {
      await load([zone()]);
      expect(pillsOn(1)).toContain('No destinations');
    });

    it('flags a disabled zone', async () => {
      await load([zone({ enabled: false, destinations: [destination()] })]);
      expect(pillsOn(1)).toContain('Disabled');
    });

    it('flags the zone a live page is running on', async () => {
      await load([zone({ destinations: [destination()] })], {
        active: [{
          page_id: 'p1', from_extension: '1513', zone_id: 1, zone_extension: '799',
          zone_name: 'All buildings', started_at: new Date().toISOString(), destinations: []
        }]
      });
      expect(pillsOn(1)).toContain('Paging now');
    });

    it('names no limit as a setting rather than leaving the cell blank', async () => {
      await load([zone({ max_duration_seconds: 0 })]);
      expect(list().textContent).toContain('No limit');
    });

    it('falls back to the server default when the zone sets no limit of its own', async () => {
      await load([zone({ max_duration_seconds: null })], {
        status: { enabled: true, default_max_duration: 90 }
      });
      expect(list().textContent).toContain('90 s');
    });
  });

  describe('the empty list, which has three different causes', () => {
    it('says paging is switched off rather than that nothing is configured', async () => {
      await load([], { status: { enabled: false } });
      expect(list().textContent).toContain('Paging is switched off');
    });

    it('distinguishes nothing configured from nothing matched', async () => {
      await load([]);
      expect(list().textContent).toContain('No paging zones yet');

      await load([zone()]);
      document.getElementById('paging-search').value = 'nothing-like-this';
      document.getElementById('paging-search').dispatchEvent(new Event('input'));
      expect(list().textContent).toContain('Nothing matches those filters');
    });

    it('offers a retry rather than claiming nothing is configured', async () => {
      await load([], { zonesStatus: 500 });
      expect(list().textContent).toContain('Could not load paging zones');
      expect(list().querySelector('[data-paging-retry]')).not.toBeNull();
    });
  });

  describe('filtering', () => {
    it('matches on a destination extension, not only the zone', async () => {
      await load([
        zone({ destinations: [destination({ endpoint_extension: '1501' })] }),
        zone({ id: 2, extension: '798', name: 'Warehouse' })
      ]);
      document.getElementById('paging-search').value = '1501';
      document.getElementById('paging-search').dispatchEvent(new Event('input'));
      expect(list().querySelectorAll('.card-shell')).toHaveLength(1);
    });

    it('keeps disabled zones visible until Enabled only is ticked', async () => {
      await load([zone(), zone({ id: 2, extension: '798', enabled: false })]);
      expect(list().querySelectorAll('.card-shell')).toHaveLength(2);

      const toggle = document.getElementById('paging-enabled-only');
      toggle.checked = true;
      toggle.dispatchEvent(new Event('change'));
      expect(list().querySelectorAll('.card-shell')).toHaveLength(1);
    });

    it('reveals the clear-filters button only while filtering', async () => {
      await load([zone()]);
      expect(document.getElementById('paging-clear-filter').hidden).toBe(true);

      document.getElementById('paging-search').value = '7';
      document.getElementById('paging-search').dispatchEvent(new Event('input'));
      expect(document.getElementById('paging-clear-filter').hidden).toBe(false);
    });
  });

  describe('the counters', () => {
    it('totals zones, their destinations, and the pages running', async () => {
      await load([
        zone({ destinations: [destination(), destination({ id: 11, endpoint_extension: '1502' })] }),
        zone({ id: 2, extension: '798' })
      ], {
        active: [{
          page_id: 'p1', from_extension: '1513', zone_id: 1, zone_extension: '799',
          zone_name: 'All buildings', started_at: new Date().toISOString(), destinations: []
        }]
      });

      expect(bodyOf('paging-zone-count').textContent).toBe('2');
      expect(bodyOf('paging-destination-count').textContent).toBe('2');
      expect(bodyOf('paging-active-count').textContent).toBe('1');
    });
  });

  describe('pages in flight', () => {
    it('hides the section entirely when nothing is paging', async () => {
      await load([zone()]);
      expect(bodyOf('paging-active-section').hidden).toBe(true);
    });

    it('reports how many destinations a live page actually reached', async () => {
      await load([zone()], {
        active: [{
          page_id: 'p1', from_extension: '1513', zone_id: 1, zone_extension: '799',
          zone_name: 'All buildings', started_at: new Date().toISOString(),
          destinations: [
            { destination_id: 1, state: 'answered' },
            { destination_id: 2, state: 'failed' },
            { destination_id: 3, state: 'pending' }
          ]
        }]
      });

      expect(bodyOf('paging-active-section').hidden).toBe(false);
      expect(activeList().textContent).toContain('1 of 3');
    });
  });

  describe('the zone dialog', () => {
    it('opens blank with the number editable when creating', async () => {
      await load([]);
      page.showAddZoneModal();

      expect(bodyOf('paging-zone-extension').value).toBe('');
      expect(bodyOf('paging-zone-extension').disabled).toBe(false);
      expect(bodyOf('paging-zone-dialog-delete').hidden).toBe(true);
    });

    it('opens populated with the number fixed when editing', async () => {
      await load([zone({ name: 'All buildings' })]);
      const edit = list().querySelector('[data-paging-edit]');
      edit.dispatchEvent(new MouseEvent('click', { bubbles: true }));

      expect(bodyOf('paging-zone-name').value).toBe('All buildings');
      // The API has no route to renumber a zone, so an editable field would
      // silently discard whatever was typed into it.
      expect(bodyOf('paging-zone-extension').disabled).toBe(true);
      expect(bodyOf('paging-zone-extension-note').textContent).toContain('cannot be changed');
    });

    it('posts a new zone and reloads', async () => {
      await load([]);
      page.showAddZoneModal();
      bodyOf('paging-zone-extension').value = '799';
      bodyOf('paging-zone-name').value = 'All buildings';
      bodyOf('paging-zone-max-duration').value = '120';

      fetch.mockClear();
      bodyOf('add-paging-zone-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      const post = fetch.mock.calls.find(([, options]) => options?.method === 'POST');
      expect(JSON.parse(post[1].body)).toMatchObject({
        extension: '799', name: 'All buildings', max_duration_seconds: 120
      });
    });

    it('omits the time limit when the box is left empty, rather than sending zero', async () => {
      await load([]);
      page.showAddZoneModal();
      bodyOf('paging-zone-extension').value = '799';
      bodyOf('paging-zone-name').value = 'All buildings';
      bodyOf('paging-zone-max-duration').value = '';

      fetch.mockClear();
      bodyOf('add-paging-zone-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      const post = fetch.mock.calls.find(([, options]) => options?.method === 'POST');
      // Empty means "use the server default"; zero means "never time out". Sending
      // zero for an empty box would uncap every zone created through this dialog.
      expect(JSON.parse(post[1].body)).not.toHaveProperty('max_duration_seconds');
    });

    it('sends zero when no limit is what was actually asked for', async () => {
      await load([]);
      page.showAddZoneModal();
      bodyOf('paging-zone-extension').value = '799';
      bodyOf('paging-zone-name').value = 'All buildings';
      bodyOf('paging-zone-max-duration').value = '0';

      fetch.mockClear();
      bodyOf('add-paging-zone-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      const post = fetch.mock.calls.find(([, options]) => options?.method === 'POST');
      expect(JSON.parse(post[1].body).max_duration_seconds).toBe(0);
    });

    it('refuses to save a zone with no name', async () => {
      await load([]);
      page.showAddZoneModal();
      bodyOf('paging-zone-extension').value = '799';

      fetch.mockClear();
      bodyOf('add-paging-zone-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      expect(fetch).not.toHaveBeenCalled();
      expect(showNotification).toHaveBeenCalledWith('The zone needs a name', 'error');
    });

    it('repeats the server’s reason for rejecting a number, rather than inventing one', async () => {
      await load([]);
      page.showAddZoneModal();
      bodyOf('paging-zone-extension').value = '1501';
      bodyOf('paging-zone-name').value = 'Clashes';

      fetch.mockImplementation(() => Promise.resolve(
        jsonResponse({ error: '1501 is already an extension' }, 409)));
      bodyOf('add-paging-zone-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      expect(showNotification).toHaveBeenCalledWith('1501 is already an extension', 'error');
    });
  });

  describe('the destination dialog', () => {
    async function openFor(zoneRow, endpoints) {
      await load([zoneRow], { endpoints });
      expand(zoneRow.id);
      const add = document.querySelector('[data-paging-add-destination]');
      add.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    }

    const options = () => [...bodyOf('paging-destination-endpoint').querySelectorAll('option')]
      .map((option) => option.value);

    it('offers every provisioned phone port', async () => {
      await openFor(zone(), [
        { extension: '1501', port: 1, vendor: 'Cisco', model: 'ATA191' },
        { extension: '1502', port: 2, vendor: 'Cisco', model: 'ATA191' }
      ]);
      expect(options()).toEqual(['1501', '1502']);
    });

    it('hides a port already in this zone, which the database would reject anyway', async () => {
      await openFor(
        zone({ destinations: [destination({ endpoint_extension: '1501' })] }),
        [{ extension: '1501', port: 1 }, { extension: '1502', port: 2 }]);
      expect(options()).toEqual(['1502']);
    });

    it('explains an empty list by pointing at provisioning', async () => {
      await openFor(zone(), []);
      expect(bodyOf('paging-destination-endpoint-note').textContent).toContain('No ATAs are provisioned');
    });

    it('distinguishes every port being used from none existing', async () => {
      await openFor(
        zone({ destinations: [destination({ endpoint_extension: '1501' })] }),
        [{ extension: '1501', port: 1 }]);
      expect(bodyOf('paging-destination-endpoint-note').textContent)
        .toContain('already a destination in this zone');
    });

    it('posts the destination it was given', async () => {
      await openFor(zone(), [{ extension: '1501', port: 1 }]);
      bodyOf('paging-destination-endpoint').value = '1501';
      bodyOf('paging-destination-label').value = 'Building 1 amp';
      bodyOf('paging-destination-dtmf').value = '2';

      fetch.mockClear();
      bodyOf('add-paging-destination-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      const post = fetch.mock.calls.find(([, opts]) => opts?.method === 'POST');
      expect(JSON.parse(post[1].body)).toMatchObject({
        kind: 'sip_endpoint',
        endpoint_extension: '1501',
        label: 'Building 1 amp',
        dtmf_sequence: '2'
      });
    });

    it('omits the sequence when the caller is meant to dial it', async () => {
      await openFor(zone(), [{ extension: '1501', port: 1 }]);
      bodyOf('paging-destination-endpoint').value = '1501';

      fetch.mockClear();
      bodyOf('add-paging-destination-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      const post = fetch.mock.calls.find(([, opts]) => opts?.method === 'POST');
      expect(JSON.parse(post[1].body)).not.toHaveProperty('dtmf_sequence');
    });

    it('blocks a sequence containing something no keypad can send', async () => {
      await openFor(zone(), [{ extension: '1501', port: 1 }]);
      bodyOf('paging-destination-endpoint').value = '1501';
      bodyOf('paging-destination-dtmf').value = '2X';

      fetch.mockClear();
      bodyOf('add-paging-destination-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      // The generator returns silence for an unknown digit and only warns, so a
      // typo stored here would page the amplifier's default circuit with no clue why.
      expect(fetch).not.toHaveBeenCalled();
      expect(showNotification).toHaveBeenCalledWith(
        'A circuit selection can only contain 0-9, * and #', 'error');
    });
  });

  describe('the test page dialog', () => {
    function openTest(zoneRow = zone()) {
      return load([zoneRow]).then(() => {
        list().querySelector('[data-paging-test]')
          .dispatchEvent(new MouseEvent('click', { bubbles: true }));
      });
    }

    it('names the zone it would page', async () => {
      await openTest(zone({ extension: '799', name: 'All buildings' }));
      expect(bodyOf('test-paging-dialog-sub').textContent).toContain('799');
      expect(bodyOf('test-paging-dialog-sub').textContent).toContain('All buildings');
    });

    it('posts the extension to ring', async () => {
      await openTest();
      bodyOf('test-paging-extension').value = '1513';

      fetch.mockClear();
      bodyOf('test-paging-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      const post = fetch.mock.calls.find(([, opts]) => opts?.method === 'POST');
      expect(String(post[0])).toContain('/api/paging/zones/1/test');
      expect(JSON.parse(post[1].body)).toEqual({ from_extension: '1513' });
    });

    it('remembers the extension between tests', async () => {
      await openTest();
      bodyOf('test-paging-extension').value = '1513';
      bodyOf('test-paging-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      // Bringing an amplifier up means paging it repeatedly from one handset.
      list().querySelector('[data-paging-test]')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
      expect(bodyOf('test-paging-extension').value).toBe('1513');
    });

    it('does not remember an extension the server rejected', async () => {
      await openTest();
      bodyOf('test-paging-extension').value = '9999';
      fetch.mockImplementation(() => Promise.resolve(
        jsonResponse({ error: 'No route to 9999' }, 400)));
      bodyOf('test-paging-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      expect(showNotification).toHaveBeenCalledWith('No route to 9999', 'error');

      list().querySelector('[data-paging-test]')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
      expect(bodyOf('test-paging-extension').value).toBe('');
    });

    it('refuses to start without an extension', async () => {
      await openTest();
      bodyOf('test-paging-extension').value = '';

      fetch.mockClear();
      bodyOf('test-paging-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      expect(fetch).not.toHaveBeenCalled();
      expect(showNotification).toHaveBeenCalledWith('Name the extension to ring', 'error');
    });

    it('reports a busy zone as the server described it', async () => {
      await openTest();
      bodyOf('test-paging-extension').value = '1513';
      fetch.mockImplementation(() => Promise.resolve(
        jsonResponse({ error: 'Building 1 amplifier is already paging' }, 409)));
      bodyOf('test-paging-form').dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }));
      await flush();

      expect(showNotification).toHaveBeenCalledWith(
        'Building 1 amplifier is already paging', 'error');
    });
  });

  describe('removing things', () => {
    it('warns how many destinations a zone deletion takes with it', async () => {
      await load([zone({ destinations: [destination(), destination({ id: 11 })] })]);
      list().querySelector('[data-paging-delete]')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));

      expect(window.confirm.mock.calls[0][0]).toContain('2 destinations');
    });

    it('does not delete when the confirmation is declined', async () => {
      window.confirm = jest.fn(() => false);
      await load([zone()]);
      fetch.mockClear();
      list().querySelector('[data-paging-delete]')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await flush();

      expect(fetch).not.toHaveBeenCalled();
    });

    it('reports the server’s refusal to delete a zone that is paging', async () => {
      await load([zone()]);
      fetch.mockImplementation(() => Promise.resolve(
        jsonResponse({ error: 'That zone is paging right now' }, 409)));

      list().querySelector('[data-paging-delete]')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await flush();

      expect(showNotification).toHaveBeenCalledWith('That zone is paging right now', 'error');
    });
  });
});
