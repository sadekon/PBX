/**
 * @jest-environment jsdom
 */

jest.mock('../js/api/client.ts', () => ({
  getAuthHeaders: jest.fn(() => ({ Authorization: 'Bearer test-token' })),
  getApiBaseUrl: jest.fn(() => 'http://localhost:9000'),
  // The module reaches for fetchWithTimeout, not fetch; delegate so tests can
  // drive both paths through the same global.fetch mock.
  fetchWithTimeout: jest.fn((url, options) => global.fetch(url, options))
}));

jest.mock('../js/ui/notifications.ts', () => ({
  showNotification: jest.fn()
}));

import { describe, it, expect, beforeEach } from '@jest/globals';

// The module keeps per-page state (which cards are expanded), so each test gets
// a fresh copy rather than inheriting the previous test's open card.
let page;

// resetModules() hands the freshly imported page its own copy of the mocked
// notifications module, so a static import here would be a different jest.fn()
// than the one the page actually calls. Resolved per-test alongside the page.
let showNotification;

global.fetch = jest.fn();

function person(overrides) {
  return {
    extension: '1001',
    name: 'Maya Rodriguez',
    email: 'm.rodriguez@acme.com',
    did_number: '+1 415 555 0172',
    registered: true,
    dnd_enabled: false,
    department: null,
    mobile: null,
    office_location: null,
    ...overrides
  };
}

function jsonResponse(body, status = 200) {
  return { ok: status < 400, status, json: async () => body };
}

function setupDom() {
  document.body.innerHTML = `
    <input type="search" id="directory-search">
    <input type="checkbox" id="directory-online-only">
    <button id="directory-clear-filter" hidden>Clear filters</button>
    <button id="directory-refresh">Refresh</button>
    <span id="directory-count"></span>
    <span id="directory-total"></span>
    <span id="directory-online"></span>
    <span id="directory-with-did"></span>
    <div id="directory-list"></div>
  `;
}

describe('Company directory page', () => {
  beforeEach(async () => {
    jest.clearAllMocks();
    jest.resetModules();
    setupDom();
    page = await import('../js/pages/phone_book.ts');
    ({ showNotification } = await import('../js/ui/notifications.ts'));
  });

  async function load(entries) {
    fetch.mockResolvedValueOnce(jsonResponse({ entries }));
    await page.loadPhoneBook();
  }

  const list = () => document.getElementById('directory-list');

  it('renders one card per person', async () => {
    await load([person(), person({ extension: '1002', name: 'Dev Patel' })]);
    expect(list().querySelectorAll('.card-shell')).toHaveLength(2);
  });

  it('builds initials from the first and last word of the name', async () => {
    await load([
      person({ name: 'Maya Rodriguez' }),
      person({ extension: '1002', name: 'Jean-Luc de Vries' }),
      person({ extension: '1003', name: 'Cher' })
    ]);
    // Listed in name order, not the order the API returned them.
    const avatars = [...list().querySelectorAll('.avatar')].map((a) => a.textContent);
    expect(avatars).toEqual(['C', 'JV', 'MR']);
  });

  it('shows presence as a dot rather than a pill when online', async () => {
    await load([person({ registered: true })]);
    expect(list().querySelector('.avatar-dot-ok')).not.toBeNull();
    // Online is the norm, so it must not also spend a pill.
    expect(list().querySelector('.pill')).toBeNull();
  });

  it('gives do-not-disturb both a warn dot and a pill', async () => {
    await load([person({ dnd_enabled: true })]);
    expect(list().querySelector('.avatar-dot-warn')).not.toBeNull();
    expect(list().querySelector('.pill').textContent).toContain('Do not disturb');
  });

  it('renders no dot at all when offline, so presence is not signalled by hue alone', async () => {
    await load([person({ registered: false })]);
    expect(list().querySelector('.avatar')).not.toBeNull();
    expect(list().querySelector('.avatar-dot')).toBeNull();
    expect(list().querySelector('.avatar').getAttribute('title')).toBe('Offline');
  });

  describe('adaptive grouping', () => {
    // The live directory has department set for a small minority, so "nobody has
    // one" is the shape this page actually renders today, not a hypothetical.
    it('renders a plain sorted list when nobody has a department', async () => {
      await load([
        person({ extension: '1003', name: 'Carol Diaz' }),
        person({ extension: '1001', name: 'Alice Chen' }),
        person({ extension: '1002', name: 'Bob Ncube' })
      ]);
      expect(list().querySelectorAll('.group-label')).toHaveLength(0);
      expect(list().querySelectorAll('.card-shell')).toHaveLength(3);
      expect(list().querySelector('.list-empty')).toBeNull();

      const names = [...list().querySelectorAll('.card-title strong')].map((n) => n.textContent);
      expect(names).toEqual(['Alice Chen', 'Bob Ncube', 'Carol Diaz']);
    });

    it('treats an empty-string department as absent rather than as a group named ""', async () => {
      await load([
        person({ extension: '1001', name: 'A A', department: '' }),
        person({ extension: '1002', name: 'B B', department: '' })
      ]);
      expect(list().querySelectorAll('.group-label')).toHaveLength(0);
      expect(list().querySelectorAll('.card-shell')).toHaveLength(2);
    });

    it('omits the enrichment rows entirely rather than listing them as unset', async () => {
      await load([person({ department: null, mobile: null, office_location: null })]);
      document.querySelector('[data-dir-toggle]').click();
      const body = list().querySelector('.card-body');

      expect(body.classList.contains('collapsed')).toBe(false);
      // Email is present on this record, so it is the only row.
      expect(body.textContent).toContain('Email');
      expect(body.textContent).not.toContain('Department');
      expect(body.textContent).not.toContain('Mobile');
      expect(body.textContent).not.toContain('Office');
      expect(body.textContent).not.toContain('null');
    });

    it('says so once when a record carries no extra detail at all', async () => {
      await load([
        person({ email: null, department: null, mobile: null, office_location: null })
      ]);
      document.querySelector('[data-dir-toggle]').click();
      const body = list().querySelector('.card-body');

      expect(body.querySelector('.muted-note')).not.toBeNull();
      expect(body.querySelectorAll('.meta-stat')).toHaveLength(0);
    });

    it('shows the empty state rather than a stray heading when there is nobody at all', async () => {
      await load([]);
      expect(list().querySelector('.list-empty').textContent).toBe('No extensions found');
      expect(list().querySelectorAll('.group-label')).toHaveLength(0);
    });

    it('stays flat when too few people have a department', async () => {
      // 1 of 4 = 25%, below the threshold.
      await load([
        person({ extension: '1001', department: 'Engineering' }),
        person({ extension: '1002', name: 'B B' }),
        person({ extension: '1003', name: 'C C' }),
        person({ extension: '1004', name: 'D D' })
      ]);
      expect(list().querySelectorAll('.group-label')).toHaveLength(0);
    });

    it('groups once most people have one', async () => {
      // 3 of 4 = 75%, above the threshold.
      await load([
        person({ extension: '1001', name: 'A A', department: 'Engineering' }),
        person({ extension: '1002', name: 'B B', department: 'Support' }),
        person({ extension: '1003', name: 'C C', department: 'Engineering' }),
        person({ extension: '1004', name: 'D D' })
      ]);
      const headings = [...list().querySelectorAll('.group-label')].map((h) => h.textContent);
      expect(headings).toEqual(['Engineering', 'Support', 'Unassigned']);
    });

    it('sorts Unassigned last even though it would sort first alphabetically', async () => {
      await load([
        person({ extension: '1001', name: 'A A', department: 'Zoology' }),
        person({ extension: '1002', name: 'B B', department: 'Zoology' }),
        person({ extension: '1003', name: 'C C', department: 'Zoology' }),
        person({ extension: '1004', name: 'D D' })
      ]);
      const headings = [...list().querySelectorAll('.group-label')].map((h) => h.textContent);
      expect(headings).toEqual(['Zoology', 'Unassigned']);
    });
  });

  describe('filtering', () => {
    beforeEach(async () => {
      await load([
        person({ extension: '1001', name: 'Maya Rodriguez', registered: true }),
        person({ extension: '1002', name: 'Dev Patel', registered: false, email: 'd.patel@acme.com' })
      ]);
    });

    function search(value) {
      const input = document.getElementById('directory-search');
      input.value = value;
      input.dispatchEvent(new Event('input'));
    }

    it('filters by name', () => {
      search('dev');
      expect(list().querySelectorAll('.card-shell')).toHaveLength(1);
      expect(list().textContent).toContain('Dev Patel');
    });

    it('filters by extension', () => {
      search('1001');
      expect(list().textContent).toContain('Maya Rodriguez');
      expect(list().textContent).not.toContain('Dev Patel');
    });

    it('reports how much of the list is showing', () => {
      search('dev');
      expect(document.getElementById('directory-count').textContent).toBe('1 of 2 shown');
    });

    it('reveals the clear button only while filtering', () => {
      const clear = document.getElementById('directory-clear-filter');
      expect(clear.hidden).toBe(true);
      search('dev');
      expect(clear.hidden).toBe(false);
      clear.click();
      expect(clear.hidden).toBe(true);
      expect(list().querySelectorAll('.card-shell')).toHaveLength(2);
    });

    it('narrows to registered extensions on request', () => {
      const checkbox = document.getElementById('directory-online-only');
      checkbox.checked = true;
      checkbox.dispatchEvent(new Event('change'));
      expect(list().querySelectorAll('.card-shell')).toHaveLength(1);
      expect(list().textContent).toContain('Maya Rodriguez');
    });

    it('says so when nothing matches', () => {
      search('nobody');
      expect(list().querySelector('.list-empty').textContent).toBe('No one matches those filters');
    });
  });

  describe('expanding a card', () => {
    beforeEach(async () => {
      await load([person({ mobile: '+1 415 555 0918', office_location: 'Portland, 3rd floor' })]);
    });

    it('surfaces mobile and office location, which appear nowhere else', () => {
      expect(list().textContent).not.toContain('Portland, 3rd floor');
      document.querySelector('[data-dir-toggle]').click();
      expect(list().textContent).toContain('+1 415 555 0918');
      expect(list().textContent).toContain('Portland, 3rd floor');
    });

    it('collapses again and drops the body content', () => {
      const toggle = document.querySelector('[data-dir-toggle]');
      toggle.click();
      toggle.click();
      expect(list().querySelector('.card-body').classList.contains('collapsed')).toBe(true);
      expect(list().textContent).not.toContain('Portland, 3rd floor');
    });

    it('tracks state in aria-expanded for screen readers', () => {
      const toggle = document.querySelector('[data-dir-toggle]');
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      toggle.click();
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
    });

    it('opens on Enter, since the title is a span rather than a real button', () => {
      const toggle = document.querySelector('[data-dir-toggle]');
      toggle.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
    });
  });

  describe('click to dial', () => {
    beforeEach(async () => {
      localStorage.setItem('pbx_extension', '2000');
      await load([person({ extension: '1001' })]);
    });

    it('rings the signed-in user first', async () => {
      fetch.mockResolvedValueOnce(jsonResponse({ success: true }));
      document.querySelector('[data-dir-dial]').click();
      await Promise.resolve();
      await Promise.resolve();

      const [url, options] = fetch.mock.calls[fetch.mock.calls.length - 1];
      expect(url).toContain('/api/framework/click-to-dial/call/2000');
      expect(JSON.parse(options.body)).toEqual({ destination: '1001' });
    });

    it('refuses to dial your own extension', async () => {
      localStorage.setItem('pbx_extension', '1001');
      const before = fetch.mock.calls.length;
      document.querySelector('[data-dir-dial]').click();
      await Promise.resolve();

      expect(fetch.mock.calls).toHaveLength(before);
      expect(showNotification).toHaveBeenCalledWith('That is your own extension', 'info');
    });
  });

  it('explains a timeout rather than showing a bare error', async () => {
    fetch.mockRejectedValueOnce(new Error('Request timed out'));
    await page.loadPhoneBook();
    expect(list().textContent).toContain('The system may still be starting');
  });

  it('counts people, online extensions and direct dial numbers', async () => {
    await load([
      person({ extension: '1001', registered: true, did_number: '+1 415 555 0172' }),
      person({ extension: '1002', registered: false, did_number: null }),
      person({ extension: '1003', registered: true, did_number: '+1 415 555 0410' })
    ]);
    expect(document.getElementById('directory-total').textContent).toBe('3');
    expect(document.getElementById('directory-online').textContent).toBe('2');
    expect(document.getElementById('directory-with-did').textContent).toBe('2');
  });
});
