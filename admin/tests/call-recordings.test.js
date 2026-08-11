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
    <input type="checkbox" id="recordings-include-voicemail">
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
    expect(list.querySelectorAll('.queue-card')).toHaveLength(1);
    expect(list.textContent).toContain('1001');
    expect(list.textContent).toContain('4m 12s');
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
