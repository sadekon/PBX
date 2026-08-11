/**
 * @jest-environment jsdom
 */

jest.mock('../js/api/client.ts', () => ({
  getAuthHeaders: jest.fn(() => ({ Authorization: 'Bearer test-token' })),
  getApiBaseUrl: jest.fn(() => 'http://localhost:9000')
}));

jest.mock('../js/ui/notifications.ts', () => ({
  showNotification: jest.fn()
}));

import { describe, it, expect, beforeEach } from '@jest/globals';

// The module keeps per-page state (which cards are expanded, which transcripts are loaded).
// Each test gets a fresh copy so one test's expanded card is not another's collapsed one.
let page;

global.fetch = jest.fn();

// jsdom implements neither of these; the module revokes and plays real elements.
global.URL.createObjectURL = jest.fn(() => 'blob:mock-url');
global.URL.revokeObjectURL = jest.fn();

const RECORDING = {
  id: 7,
  session_id: 'sess-1',
  call_id: 'call-1',
  kind: 'recording',
  duration_seconds: 252,
  participants: ['1001', '1002'],
  started_at: '2026-08-10T14:02:00Z',
  ended_at: null,
  audio_deleted_at: null,
  created_at: '2026-08-10T14:02:00Z'
};

function jsonResponse(body, status = 200) {
  return { ok: status < 400, status, json: async () => body };
}

function blobResponse() {
  return { ok: true, status: 200, blob: async () => new Blob(['RIFF'], { type: 'audio/wav' }) };
}

function setupDom() {
  document.body.innerHTML = `
    <input type="text" id="recordings-participant-filter">
    <button id="recordings-clear-filter" hidden>Clear</button>
    <input type="checkbox" id="recordings-include-voicemail">
    <span id="recordings-summary"></span>
    <div id="call-recordings-list"></div>
  `;
}

/** Expand a card by clicking its title, then let the queued audio fetch settle. */
async function expandFirstCard() {
  document.querySelector('[data-rec-toggle]').click();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe('Call recordings page', () => {
  beforeEach(async () => {
    jest.clearAllMocks();
    jest.resetModules();
    setupDom();
    global.URL.createObjectURL.mockReturnValue('blob:mock-url');
    page = await import('../js/pages/call-recordings.ts');
  });

  /** Load the list and bind the delegated click handler, as the tab loader does. */
  async function load(recordingsPayload) {
    fetch.mockResolvedValueOnce(jsonResponse(recordingsPayload));
    page.initCallRecordings();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  }

  it('lists recordings as cards', async () => {
    await load({ recordings: [RECORDING], count: 1 });

    const list = document.getElementById('call-recordings-list');
    expect(list.querySelectorAll('.card-shell')).toHaveLength(1);
    expect(list.textContent).toContain('1001');
    expect(list.textContent).toContain('4m 12s');
  });

  it('groups cards under a day heading and drops the date from the row', async () => {
    await load({ recordings: [RECORDING], count: 1 });

    const list = document.getElementById('call-recordings-list');
    expect(list.querySelectorAll('.group-label')).toHaveLength(1);
    // The full date belongs to the heading; the row carries time of day only.
    expect(list.querySelector('.card-head').textContent).not.toMatch(/2026/);
  });

  it('shows no pill for the ordinary case', async () => {
    // "Audio available" on every row is the norm restated once per card.
    await load({ recordings: [RECORDING], count: 1 });
    expect(document.querySelectorAll('.pill')).toHaveLength(0);
  });

  it('pills expired audio and an available transcript', async () => {
    await load({
      recordings: [{ ...RECORDING, audio_deleted_at: '2026-01-01T00:00:00Z', has_transcript: true }]
    });

    const text = document.querySelector('.card-head').textContent;
    expect(text).toContain('Audio expired');
    expect(text).toContain('Transcript');
  });

  it('reports the result count when the list is complete', async () => {
    await load({ recordings: [RECORDING, { ...RECORDING, id: 8 }], count: 2, has_more: false });
    expect(document.getElementById('recordings-summary').textContent).toBe('2 recordings');
    expect(document.getElementById('recordings-load-more')).toBeNull();
  });

  it('offers Load more only while pages remain', async () => {
    await load({ recordings: [RECORDING], count: 1, has_more: true, next_before: 7 });

    expect(document.getElementById('recordings-load-more')).not.toBeNull();
    expect(document.getElementById('recordings-summary').textContent).toBe('1 recording loaded');
  });

  it('appends the next page rather than replacing what is on screen', async () => {
    await load({ recordings: [RECORDING], count: 1, has_more: true, next_before: 7 });

    fetch.mockResolvedValueOnce(jsonResponse({
      recordings: [{ ...RECORDING, id: 8 }], count: 1, has_more: false
    }));
    document.getElementById('recordings-load-more').click();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    // Both pages are on screen, and the cursor was sent so the server knows where to resume.
    expect(document.querySelectorAll('.card-shell')).toHaveLength(2);
    expect(fetch.mock.calls[1][0]).toContain('before=7');
    expect(document.getElementById('recordings-load-more')).toBeNull();
  });

  it('reveals Clear only while the filter has a value', async () => {
    await load({ recordings: [], count: 0 });

    const input = document.getElementById('recordings-participant-filter');
    const clear = document.getElementById('recordings-clear-filter');
    expect(clear.hidden).toBe(true);

    input.value = '1001';
    input.dispatchEvent(new Event('input'));
    expect(clear.hidden).toBe(false);

    fetch.mockResolvedValueOnce(jsonResponse({ recordings: [], count: 0 }));
    clear.click();
    await Promise.resolve();

    expect(input.value).toBe('');
    expect(clear.hidden).toBe(true);
  });

  it('does not send a cursor on the first page', async () => {
    await load({ recordings: [RECORDING], count: 1, has_more: true, next_before: 7 });
    expect(fetch.mock.calls[0][0]).not.toContain('before=');
  });

  it('requests calls only, without voicemail, by default', async () => {
    await load({ recordings: [], count: 0 });

    expect(fetch.mock.calls[0][0]).not.toContain('include_voicemail');
  });

  it('passes the participant filter to the server', async () => {
    document.getElementById('recordings-participant-filter').value = '1001';
    await load({ recordings: [], count: 0 });

    expect(fetch.mock.calls[0][0]).toContain('participant=1001');
  });

  it('never renders an audio element without a source', async () => {
    // The regression this guards: an <audio controls> with no src renders a working-looking
    // player whose own play button reports "no supported source was found".
    await load({ recordings: [RECORDING], count: 1 });

    fetch.mockResolvedValueOnce(blobResponse());
    await expandFirstCard();

    const players = document.querySelectorAll('audio');
    expect(players.length).toBeGreaterThan(0);
    for (const player of players) {
      expect(player.getAttribute('src')).toBeTruthy();
    }
  });

  it('shows the server reason instead of an empty player when audio will not load', async () => {
    await load({ recordings: [RECORDING], count: 1 });

    // 415: a format the server refuses to send rather than serve unplayable bytes.
    fetch.mockResolvedValueOnce(jsonResponse({ error: 'bad format' }, 415));
    await expandFirstCard();

    expect(document.querySelectorAll('audio')).toHaveLength(0);
    expect(document.getElementById('call-recordings-list').textContent)
      .toContain('format the browser cannot play');
  });

  it('does not render a player at all once audio has expired', async () => {
    await load({ recordings: [{ ...RECORDING, audio_deleted_at: '2026-01-01T00:00:00Z' }] });
    await expandFirstCard();

    expect(document.querySelectorAll('audio')).toHaveLength(0);
    expect(document.getElementById('call-recordings-list').textContent)
      .toContain('removed by the retention policy');
  });

  it('explains a 403 as the participant_access default rather than a failure', async () => {
    await load({ error: 'nope' });
    // load() mocks a 200; re-run with the 403 the server actually sends.
    fetch.mockResolvedValueOnce(jsonResponse({ error: 'nope' }, 403));
    await page.loadCallRecordings();

    expect(document.getElementById('call-recordings-list').textContent)
      .toContain('admin-only unless participant access is enabled');
  });
});
