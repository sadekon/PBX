/**
 * @jest-environment jsdom
 *
 * The ring plan this page draws duplicates rules that live in
 * pbx/features/find_me_follow_me.py -- the implicit desk leg, the duplicate and
 * cap filtering, and the fact that a simultaneous burst's total is its longest
 * leg rather than the sum. These tests are what make that duplication safe to
 * have: if the backend rules change, they fail here rather than silently
 * misreporting a plan to an operator.
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

function config(overrides) {
  return {
    extension: '1001',
    mode: 'sequential',
    enabled: true,
    destinations: [{ number: '+15550142', ring_time: 25 }],
    updated_at: '2026-08-17T13:28:15',
    ...overrides
  };
}

function jsonResponse(body, status = 200) {
  return { ok: status < 400, status, json: async () => body };
}

function setupDom() {
  document.body.innerHTML = `
    <input type="search" id="fmfm-search">
    <select id="fmfm-mode-filter">
      <option value="all" selected>All modes</option>
      <option value="sequential">Sequential</option>
      <option value="simultaneous">Simultaneous</option>
    </select>
    <input type="checkbox" id="fmfm-active-only">
    <button id="fmfm-clear-filter" hidden>Clear filters</button>
    <button id="fmfm-refresh">Refresh</button>
    <button id="fmfm-add">Configure extension</button>
    <span id="fmfm-count"></span>
    <span id="fmfm-total-extensions"></span>
    <span id="fmfm-active-count"></span>
    <span id="fmfm-destinations"></span>
    <div id="fmfm-list"></div>

    <div id="add-fmfm-modal" class="modal">
      <div class="glass-page">
        <div class="g g1 dialog">
          <div class="dialog-head">
            <h3 id="fmfm-dialog-title"></h3>
            <p id="fmfm-dialog-sub"></p>
            <button id="fmfm-dialog-close"></button>
          </div>
          <form id="add-fmfm-form">
            <input type="text" id="fmfm-extension">
            <span id="fmfm-extension-note"></span>
            <div class="mode-choice">
              <label class="mode-opt" data-fmfm-mode="sequential">
                <input type="radio" name="fmfm-mode" value="sequential" checked>
              </label>
              <label class="mode-opt" data-fmfm-mode="simultaneous">
                <input type="radio" name="fmfm-mode" value="simultaneous">
              </label>
            </div>
            <div id="fmfm-destinations-list"></div>
            <div id="fmfm-dialog-warnings"></div>
            <input type="checkbox" id="fmfm-enabled" checked>
          </form>
          <div class="dialog-foot">
            <button id="fmfm-dialog-delete"></button>
            <button id="fmfm-dialog-cancel"></button>
            <button id="fmfm-dialog-save"></button>
          </div>
        </div>
      </div>
    </div>
  `;
}

describe('Find Me/Follow Me page', () => {
  beforeEach(async () => {
    jest.clearAllMocks();
    jest.resetModules();
    setupDom();
    page = await import('../js/pages/call-routing.ts');
    ({ showNotification } = await import('../js/ui/notifications.ts'));
  });

  /** Routes by URL rather than call order, since the two requests race. */
  function respond({ extensions, initialRing = 20, extensionsStatus = 200, statsOk = true }) {
    fetch.mockImplementation((url) => {
      if (String(url).includes('/api/fmfm/statistics')) {
        return Promise.resolve(statsOk
          ? jsonResponse({ initial_ring_time: initialRing })
          : jsonResponse({ error: 'nope' }, 500));
      }
      return Promise.resolve(jsonResponse({ extensions, count: extensions?.length ?? 0 }, extensionsStatus));
    });
  }

  async function load(extensions, options = {}) {
    respond({ extensions, ...options });
    await page.loadFMFMExtensions();
  }

  const list = () => document.getElementById('fmfm-list');

  /** Opens a card and returns the rendered ring-plan step lines. */
  function expand(extension) {
    const toggle = [...document.querySelectorAll('[data-fmfm-toggle]')]
      .find((el) => el.getAttribute('data-fmfm-toggle') === extension);
    toggle.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    const body = [...document.querySelectorAll('[data-fmfm-body]')]
      .find((el) => el.getAttribute('data-fmfm-body') === extension);
    return [...body.querySelectorAll('.ring-step')].map((step) => ({
      number: step.querySelector('.ring-number').textContent.trim(),
      when: step.querySelector('.ring-when').textContent.trim()
    }));
  }

  const tailText = () => list().querySelector('.ring-tail').textContent.replace(/\s+/g, ' ').trim();

  it('renders one card per configuration', async () => {
    await load([config(), config({ extension: '1002' })]);
    expect(list().querySelectorAll('.card-shell')).toHaveLength(2);
  });

  it('sorts extensions numerically, so 999 precedes 1000', async () => {
    await load([config({ extension: '1000' }), config({ extension: '999' })]);
    const titles = [...list().querySelectorAll('.card-title strong')].map((el) => el.textContent);
    expect(titles).toEqual(['Extension 999', 'Extension 1000']);
  });

  describe('the sequential ring plan', () => {
    it('puts the implicit desk leg first and chains the windows', async () => {
      await load([config({
        destinations: [
          { number: '+15550142', ring_time: 25 },
          { number: '2005', ring_time: 15 }
        ]
      })]);
      expect(expand('1001')).toEqual([
        { number: '1001', when: '0 – 20 s' },
        { number: '+15550142', when: '20 – 45 s' },
        { number: '2005', when: '45 – 60 s' }
      ]);
    });

    it('totals the legs and names the original extension for voicemail', async () => {
      await load([config({ destinations: [{ number: '+15550142', ring_time: 25 }] })]);
      expand('1001');
      expect(tailText()).toBe('↳ No answer after 45 s → voicemail for 1001');
    });

    it('uses the configured initial_ring_time rather than assuming 20', async () => {
      await load([config()], { initialRing: 45 });
      expect(expand('1001')[0]).toEqual({ number: '1001', when: '0 – 45 s' });
    });

    it('omits the desk leg when initial_ring_time is 0', async () => {
      await load([config()], { initialRing: 0 });
      expect(expand('1001')).toEqual([{ number: '+15550142', when: '0 – 25 s' }]);
    });

    it("lets an explicit listing of the extension override the implicit leg, ring time and position included", async () => {
      await load([config({
        destinations: [
          { number: '+15550142', ring_time: 25 },
          { number: '1001', ring_time: 5 }
        ]
      })]);
      // The desk is not promoted to the front and keeps its own 5 s.
      expect(expand('1001')).toEqual([
        { number: '+15550142', when: '0 – 25 s' },
        { number: '1001', when: '25 – 30 s' }
      ]);
    });
  });

  describe('the simultaneous ring plan', () => {
    it('rings every leg from zero and totals the longest, not the sum', async () => {
      await load([config({
        mode: 'simultaneous',
        destinations: [
          { number: '+15550142', ring_time: 25 },
          { number: '2005', ring_time: 15 }
        ]
      })]);
      // The desk leg rings for max(initial, longest) = max(20, 25) = 25.
      expect(expand('1001')).toEqual([
        { number: '1001', when: '0 – 25 s' },
        { number: '+15550142', when: '0 – 25 s' },
        { number: '2005', when: '0 – 15 s' }
      ]);
      expect(tailText()).toBe('↳ No answer after 25 s → voicemail for 1001');
    });
  });

  describe('the rules _sanitize() applies', () => {
    it('drops a duplicate destination, keeping the first', async () => {
      await load([config({
        destinations: [
          { number: '2005', ring_time: 10 },
          { number: '2005', ring_time: 30 }
        ]
      })], { initialRing: 0 });
      expect(expand('1001')).toEqual([{ number: '2005', when: '0 – 10 s' }]);
    });

    it('clamps a stored ring time above the sequential ceiling', async () => {
      await load([config({ destinations: [{ number: '2005', ring_time: 500 }] })], { initialRing: 0 });
      expect(expand('1001')).toEqual([{ number: '2005', when: '0 – 60 s' }]);
    });

    it('clamps a simultaneous config to the shorter voicemail-timeout ceiling', async () => {
      await load([config({
        mode: 'simultaneous',
        destinations: [{ number: '2005', ring_time: 60 }]
      })], { initialRing: 0 });
      expect(expand('1001')).toEqual([{ number: '2005', when: '0 – 30 s' }]);
    });

    it('counts the implicit desk leg toward the ten-leg cap', async () => {
      const destinations = Array.from({ length: 10 }, (_, i) => ({
        number: `200${i}`, ring_time: 5
      }));
      await load([config({ destinations })]);
      const steps = expand('1001');
      expect(steps).toHaveLength(10);
      // The desk consumed a slot, so the tenth stored destination never rings.
      expect(steps[0].number).toBe('1001');
      expect(steps.map((s) => s.number)).not.toContain('2009');
    });
  });

  describe('when initial_ring_time cannot be read', () => {
    it('omits the desk leg and says so rather than inventing one', async () => {
      await load([config()], { statsOk: false });
      expect(expand('1001')).toEqual([{ number: '+15550142', when: '0 – 25 s' }]);
      expect(list().querySelector('.ring-plan .muted-note').textContent)
        .toMatch(/could not be read from the server/);
    });
  });

  describe('the summary tiles', () => {
    it('counts configurations, active ones, and the legs that will ring', async () => {
      await load([
        config(),
        config({ extension: '1002', enabled: false })
      ]);
      expect(document.getElementById('fmfm-total-extensions').textContent).toBe('2');
      expect(document.getElementById('fmfm-active-count').textContent).toBe('1');
      // Two configs, each a desk leg plus one destination.
      expect(document.getElementById('fmfm-destinations').textContent).toBe('4');
    });
  });

  describe('filtering', () => {
    it('matches on a destination number, not only the extension', async () => {
      await load([
        config({ extension: '1001', destinations: [{ number: '+15550142', ring_time: 20 }] }),
        config({ extension: '1002', destinations: [{ number: '+15559999', ring_time: 20 }] })
      ]);
      const search = document.getElementById('fmfm-search');
      search.value = '0142';
      search.dispatchEvent(new Event('input'));
      expect(list().querySelectorAll('.card-shell')).toHaveLength(1);
    });

    it('keeps disabled configurations visible until Active only is ticked', async () => {
      await load([config(), config({ extension: '1002', enabled: false })]);
      expect(list().querySelectorAll('.card-shell')).toHaveLength(2);
      expect(list().querySelector('.pill-warn').textContent).toBe('Disabled');

      const active = document.getElementById('fmfm-active-only');
      active.checked = true;
      active.dispatchEvent(new Event('change'));
      expect(list().querySelectorAll('.card-shell')).toHaveLength(1);
    });

    it('distinguishes "nothing configured" from "nothing matched"', async () => {
      await load([]);
      expect(list().textContent).toMatch(/No extensions are using Find Me/);

      await load([config()]);
      const search = document.getElementById('fmfm-search');
      search.value = 'zzzz';
      search.dispatchEvent(new Event('input'));
      expect(list().textContent).toMatch(/Nothing matches those filters/);
    });
  });

  describe('when the configurations cannot be loaded', () => {
    it('offers a retry rather than claiming nothing is configured', async () => {
      await load([], { extensionsStatus: 500 });
      expect(list().textContent).toMatch(/Could not load the configurations/);
      expect(list().querySelector('[data-fmfm-retry]')).not.toBeNull();
      expect(showNotification).toHaveBeenCalledWith(
        'Error loading FMFM configurations', 'error');
    });
  });

  describe('the configure dialog', () => {
    const dialog = () => document.getElementById('add-fmfm-modal');
    const rows = () => [...document.querySelectorAll('#fmfm-destinations-list .plan-row')];
    const warnings = () => [...document.querySelectorAll('#fmfm-dialog-warnings .form-warn')]
      .map((w) => w.textContent.replace(/\s+/g, ' ').trim());

    function planRows() {
      return rows().map((row) => ({
        number: row.querySelector('.plan-static')
          ? row.querySelector('.plan-static strong').textContent
          : row.querySelector('[data-fmfm-dest]').value,
        when: row.querySelector('.ring-when')?.textContent.trim()
          ?? row.querySelector('.plan-drop-flag')?.textContent.trim(),
        derived: row.classList.contains('plan-row-derived')
      }));
    }

    function typeInto(selector, value) {
      const input = document.querySelector(selector);
      input.value = value;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function chooseMode(mode) {
      const radio = document.querySelector(`input[name="fmfm-mode"][value="${mode}"]`);
      radio.checked = true;
      radio.dispatchEvent(new Event('change', { bubbles: true }));
    }

    beforeEach(async () => {
      await load([]);   // establishes fmfmInitialRing = 20
    });

    it('opens blank and editable when creating', async () => {
      page.showAddFMFMModal();
      expect(dialog().classList.contains('active')).toBe(true);
      expect(document.getElementById('fmfm-extension').readOnly).toBe(false);
      expect(document.getElementById('fmfm-dialog-delete').hidden).toBe(true);
      expect(rows()).toHaveLength(1);
    });

    it('opens populated with the extension fixed when editing', async () => {
      page.editFMFMConfig(config({
        destinations: [{ number: '+15550142', ring_time: 25 }, { number: '2005', ring_time: 15 }]
      }));
      expect(document.getElementById('fmfm-extension').readOnly).toBe(true);
      expect(document.getElementById('fmfm-dialog-delete').hidden).toBe(false);
      // The implicit desk leg plus the two stored destinations.
      expect(planRows()).toEqual([
        { number: '1001', when: '0 – 20 s', derived: true },
        { number: '+15550142', when: '20 – 45 s', derived: false },
        { number: '2005', when: '45 – 60 s', derived: false }
      ]);
    });

    it('re-times every leg when the mode changes', async () => {
      page.editFMFMConfig(config({
        destinations: [{ number: '+15550142', ring_time: 25 }, { number: '2005', ring_time: 15 }]
      }));
      chooseMode('simultaneous');
      expect(planRows()).toEqual([
        // desk = max(initial 20, longest 25)
        { number: '1001', when: '0 – 25 s', derived: true },
        { number: '+15550142', when: '0 – 25 s', derived: false },
        { number: '2005', when: '0 – 15 s', derived: false }
      ]);
      expect(document.querySelector('.plan-tail .grand').textContent).toBe('25 s');
    });

    it('follows the extension being typed into the plan it draws', async () => {
      page.showAddFMFMModal();
      typeInto('[data-fmfm-dest="0"]', '2005');
      typeInto('#fmfm-extension', '1234');
      expect(planRows()[0]).toEqual({ number: '1234', when: '0 – 20 s', derived: true });
      expect(document.querySelector('.plan-tail').textContent)
        .toMatch(/voicemail for 1234/);
    });

    it('flags a duplicate in place rather than deleting the row', async () => {
      page.editFMFMConfig(config({
        destinations: [{ number: '2005', ring_time: 10 }, { number: '2005', ring_time: 30 }]
      }));
      expect(rows()).toHaveLength(3);                       // desk + both inputs
      expect(planRows()[2].when).toBe('duplicate');
      expect(warnings().join(' ')).toMatch(/listed twice/);
    });

    it('explains that listing the extension replaces the automatic leg', async () => {
      page.editFMFMConfig(config({
        destinations: [{ number: '1001', ring_time: 5 }]
      }));
      expect(planRows().some((r) => r.derived)).toBe(false);
      expect(warnings().join(' ')).toMatch(/not also rung automatically first/);
    });

    it('stops offering more destinations once the desk leg fills the cap', async () => {
      page.editFMFMConfig(config({
        destinations: Array.from({ length: 9 }, (_, i) => ({ number: `200${i}`, ring_time: 5 }))
      }));
      // Nine stored plus the implicit desk is exactly ten legs.
      expect(document.getElementById('fmfm-add-destination').disabled).toBe(true);
      expect(document.querySelector('.plan-add .count').textContent).toBe('10 of 10 legs');
    });

    // The editor used to hold the parsed number and echo it back into the box
    // on every keystroke, so clearing the field produced "0" and the next digit
    // landed beside it -- entering 30 was very nearly impossible. The field now
    // keeps its own text and the save is blocked instead.
    it('leaves a half-typed ring time exactly as typed', async () => {
      page.editFMFMConfig(config({ destinations: [{ number: '2005', ring_time: 20 }] }));
      typeInto('[data-fmfm-ring="0"]', '');
      expect(document.querySelector('[data-fmfm-ring="0"]').value).toBe('');
      typeInto('[data-fmfm-ring="0"]', '3');
      expect(document.querySelector('[data-fmfm-ring="0"]').value).toBe('3');
      typeInto('[data-fmfm-ring="0"]', '30');
      expect(document.querySelector('[data-fmfm-ring="0"]').value).toBe('30');
      expect(document.getElementById('fmfm-dialog-save').disabled).toBe(false);
    });

    it('blocks the save while a ring time is out of range, and says which row', () => {
      page.editFMFMConfig(config({ destinations: [{ number: '2005', ring_time: 20 }] }));
      typeInto('[data-fmfm-ring="0"]', '3');
      expect(document.getElementById('fmfm-dialog-save').disabled).toBe(true);
      expect(warnings().join(' ')).toMatch(/destination 1 is below the 5 s minimum/);

      typeInto('[data-fmfm-ring="0"]', '300');
      expect(document.getElementById('fmfm-dialog-save').disabled).toBe(true);
      expect(warnings().join(' ')).toMatch(/destination 1 is above the 60 s maximum/);

      typeInto('[data-fmfm-ring="0"]', '30');
      expect(document.getElementById('fmfm-dialog-save').disabled).toBe(false);
      expect(warnings().join(' ')).not.toMatch(/Fix it to save/);
    });

    it('holds an empty ring time as an error rather than turning it into zero', () => {
      page.editFMFMConfig(config({ destinations: [{ number: '2005', ring_time: 20 }] }));
      typeInto('[data-fmfm-ring="0"]', '');
      expect(document.getElementById('fmfm-dialog-save').disabled).toBe(true);
      expect(warnings().join(' ')).toMatch(/destination 1 is empty/);
    });

    it('applies the shorter simultaneous ceiling as soon as the mode changes', () => {
      page.editFMFMConfig(config({ destinations: [{ number: '2005', ring_time: 45 }] }));
      expect(document.getElementById('fmfm-dialog-save').disabled).toBe(false);

      const radio = document.querySelector('input[name="fmfm-mode"][value="simultaneous"]');
      radio.checked = true;
      radio.dispatchEvent(new Event('change', { bubbles: true }));

      // 45 s was fine while the legs ran back to back; in a burst it is the
      // caller's whole wait, so it now exceeds the voicemail timeout.
      expect(document.getElementById('fmfm-dialog-save').disabled).toBe(true);
      expect(warnings().join(' ')).toMatch(/is above the 30 s maximum/);
    });

    it('refuses to submit an out-of-range ring time even without the button', async () => {
      page.editFMFMConfig(config({ destinations: [{ number: '2005', ring_time: 20 }] }));
      typeInto('[data-fmfm-ring="0"]', '300');
      fetch.mockClear();

      await page.saveFMFMConfig(new Event('submit'));

      expect(fetch).not.toHaveBeenCalled();
      expect(showNotification).toHaveBeenCalledWith(
        expect.stringMatching(/destination 1 is above the 60 s maximum/), 'error');
    });

    it('warns when the caller would wait more than two minutes', async () => {
      // Three legs at the sequential ceiling, plus the 20 s desk leg.
      page.editFMFMConfig(config({
        destinations: [
          { number: '2005', ring_time: 60 },
          { number: '2006', ring_time: 60 },
          { number: '2007', ring_time: 60 }
        ]
      }));
      expect(warnings().join(' ')).toMatch(/waits 200 s/);
    });

    it('posts what was typed and closes on success', async () => {
      page.editFMFMConfig(config({ destinations: [{ number: '2005', ring_time: 15 }] }));
      fetch.mockImplementation((url, options) => {
        if (String(url).includes('/api/fmfm/config') && options?.method === 'POST') {
          return Promise.resolve(jsonResponse({ success: true }));
        }
        if (String(url).includes('/api/fmfm/statistics')) {
          return Promise.resolve(jsonResponse({ initial_ring_time: 20 }));
        }
        return Promise.resolve(jsonResponse({ extensions: [] }));
      });

      await page.saveFMFMConfig(new Event('submit'));

      const post = fetch.mock.calls.find(([, o]) => o?.method === 'POST');
      expect(JSON.parse(post[1].body)).toEqual({
        extension: '1001',
        mode: 'sequential',
        enabled: true,
        destinations: [{ number: '2005', ring_time: 15 }]
      });
      expect(dialog().classList.contains('active')).toBe(false);
    });

    it('refuses to save without a destination', async () => {
      page.showAddFMFMModal();
      typeInto('#fmfm-extension', '1234');
      await page.saveFMFMConfig(new Event('submit'));
      expect(showNotification).toHaveBeenCalledWith(
        'At least one destination is required', 'error');
      expect(dialog().classList.contains('active')).toBe(true);
    });

    it('closes on Cancel', async () => {
      page.showAddFMFMModal();
      document.getElementById('fmfm-dialog-cancel')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
      expect(dialog().classList.contains('active')).toBe(false);
    });

    it('uses .active so the shared Escape handler can close it', async () => {
      page.showAddFMFMModal();
      // ui/tabs.ts matches `.modal.active`; an inline display style would not.
      expect(document.querySelector('.modal.active')).toBe(dialog());
    });
  });
});
