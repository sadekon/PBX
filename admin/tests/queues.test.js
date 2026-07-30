/**
 * @jest-environment jsdom
 */

// Mock dependency modules before importing the module under test.
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
  loadQueuesData,
  loadQueues,
  loadQueueAgents,
  setQueueAgentState,
  deleteQueue,
  removeQueueAgent,
  toggleQueueCollapse
} from '../js/pages/queues.ts';
import { showNotification } from '../js/ui/notifications.ts';

global.fetch = jest.fn();
global.confirm = jest.fn(() => true);

// The queues page renders one card per queue (not a single table): the
// queue's own attributes live in the card header, its agents in a sub-table.
document.body.innerHTML = `
  <div id="queues-cards"></div>
`;

const container = () => document.getElementById('queues-cards');

const QUEUE = {
  queue_number: '8001',
  name: 'Sales',
  strategy: 'round_robin',
  enabled: true,
  ring_timeout: 15,
  max_wait_time: 300,
  max_queue_size: 10,
  fallback_mailbox: '8001',
  auto_pause_misses: 3,
  members: ['1001', '1002'],
  calls_waiting: 2,
  longest_wait: 33.4,
  total_agents: 2,
  available_agents: 1
};

const AGENT = {
  extension: '1001',
  logged_in: true,
  paused: false,
  pause_reason: null,
  consecutive_misses: 0,
  calls_taken: 5,
  last_call_time: '2026-07-23T10:00:00+00:00',
  queues: ['8001']
};

// Queue every fetch response the mock should hand out, in call order.
function mockFetchSequence(...responses) {
  for (const r of responses) {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => r });
  }
}

describe('Call Queues page', () => {
  beforeEach(async () => {
    jest.clearAllMocks();
    global.confirm.mockReturnValue(true);
    // Reset the module-level caches to a known-empty baseline so each test
    // starts from a clean slate, then clear the DOM.
    mockFetchSequence({ queues: [] }, { agents: [] });
    await loadQueuesData();
    jest.clearAllMocks();
    container().innerHTML = '';
  });

  describe('loadQueuesData (card render)', () => {
    it('renders a queue card with its agent sub-table', async () => {
      // loadQueuesData fetches queues first, then agent states.
      mockFetchSequence({ queues: [QUEUE] }, { agents: [AGENT] });

      await loadQueuesData();

      const html = container().innerHTML;
      // Queue card header: identity + attributes as labeled stats
      expect(html).toContain('8001');
      expect(html).toContain('Sales');
      expect(html).toContain('Enabled');
      expect(html).toContain('round_robin');
      expect(html).toContain('1 / 2'); // Online x / y
      // Agent sub-table with live state, shown as a status pill
      expect(html).toContain('1001');
      expect(html).toContain('status-pill');
      expect(html).toContain('Available');
      expect(html).toContain('Log out');
      // Member without a live state entry still appears (defaults to logged out)
      expect(html).toContain('1002');
      // Per-queue removal is available on each agent row
      expect(html).toContain('Remove');
      expect(html).toContain("removeQueueAgent('8001', '1001')");
      // Card title is a collapse toggle
      expect(html).toContain("toggleQueueCollapse('8001')");
      // "Add agent" lives beneath the agent table, not the header
      expect(html).toContain('add-agent-btn');
      expect(html).toContain("showAddQueueAgentModal('8001')");
    });

    it('collapses and expands a queue card', async () => {
      mockFetchSequence({ queues: [QUEUE] }, { agents: [AGENT] });
      await loadQueuesData();

      const body = () => document.querySelector('[data-queue-body="8001"]');
      // Expanded by default
      expect(body().classList.contains('collapsed')).toBe(false);

      toggleQueueCollapse('8001');
      expect(body().classList.contains('collapsed')).toBe(true);
      expect(
        document.querySelector('[data-queue-toggle="8001"]').getAttribute('aria-expanded')
      ).toBe('false');

      toggleQueueCollapse('8001');
      expect(body().classList.contains('collapsed')).toBe(false);
    });

    it('keeps a card collapsed across re-render', async () => {
      mockFetchSequence({ queues: [QUEUE] }, { agents: [AGENT] });
      await loadQueuesData();
      toggleQueueCollapse('8001');

      // A subsequent action triggers a full re-render; collapse state persists.
      mockFetchSequence({ queues: [QUEUE] }, { agents: [AGENT] });
      await loadQueuesData();

      expect(
        document.querySelector('[data-queue-body="8001"]').classList.contains('collapsed')
      ).toBe(true);

      // Restore expanded state so the shared module-level set stays clean.
      toggleQueueCollapse('8001');
    });

    it('renders empty state when there are no queues', async () => {
      mockFetchSequence({ queues: [] }, { agents: [] });

      await loadQueuesData();

      expect(container().innerHTML).toContain('No call queues configured');
    });

    it('shows an empty-state hint plus the add affordance for a queue with no members', async () => {
      mockFetchSequence({ queues: [{ ...QUEUE, members: [] }] }, { agents: [] });

      await loadQueuesData();

      const html = container().innerHTML;
      expect(html).toContain('No agents assigned');
      expect(html).toContain('add-agent-btn');
    });

    it('shows paused status with reason for an agent', async () => {
      mockFetchSequence(
        { queues: [QUEUE] },
        { agents: [{ ...AGENT, paused: true, pause_reason: 'auto_missed' }] }
      );

      await loadQueuesData();

      expect(container().innerHTML).toContain('Paused (auto_missed)');
    });

    it('shows the Disabled state for a disabled queue', async () => {
      mockFetchSequence({ queues: [{ ...QUEUE, enabled: false }] }, { agents: [] });

      await loadQueuesData();

      expect(container().innerHTML).toContain('Disabled');
    });
  });

  describe('loadQueues / loadQueueAgents (targeted refresh)', () => {
    it('loadQueues re-renders the cards from cached queues', async () => {
      mockFetchSequence({ queues: [QUEUE] });

      await loadQueues();

      const html = container().innerHTML;
      expect(html).toContain('8001');
      expect(html).toContain('Sales');
      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:9000/api/queues',
        expect.objectContaining({ headers: expect.any(Object) })
      );
    });

    it('loadQueues keeps prior data on HTTP error (renders from cache)', async () => {
      // Seed a queue, then fail the next refresh.
      mockFetchSequence({ queues: [QUEUE] });
      await loadQueues();
      global.fetch.mockResolvedValueOnce({ ok: false, status: 500 });

      await loadQueues();

      // Cached queue is still shown rather than being wiped.
      expect(container().innerHTML).toContain('8001');
    });

    it('loadQueueAgents refreshes agent status in the card', async () => {
      mockFetchSequence({ queues: [QUEUE] });
      await loadQueues();
      mockFetchSequence({ agents: [AGENT] });

      await loadQueueAgents();

      expect(container().innerHTML).toContain('Available');
      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:9000/api/queues/agents/state',
        expect.objectContaining({ headers: expect.any(Object) })
      );
    });
  });

  describe('setQueueAgentState', () => {
    it('PUTs state and notifies on success', async () => {
      global.fetch.mockResolvedValue({
        ok: true,
        json: async () => ({ success: true, queues: [], agents: [] })
      });

      await setQueueAgentState('1001', { logged_in: false });

      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:9000/api/queues/agents/1001/state',
        expect.objectContaining({
          method: 'PUT',
          body: JSON.stringify({ logged_in: false })
        })
      );
      expect(showNotification).toHaveBeenCalledWith('Agent 1001 updated', 'success');
    });

    it('notifies error message from API', async () => {
      global.fetch.mockResolvedValueOnce({
        ok: false,
        json: async () => ({ error: 'not a member of any queue' })
      });

      await setQueueAgentState('2000', { logged_in: true });

      expect(showNotification).toHaveBeenCalledWith('not a member of any queue', 'error');
    });
  });

  describe('deleteQueue', () => {
    it('DELETEs after confirmation', async () => {
      global.fetch.mockResolvedValue({
        ok: true,
        json: async () => ({ success: true, queues: [], agents: [] })
      });

      await deleteQueue('8001');

      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:9000/api/queues/8001',
        expect.objectContaining({ method: 'DELETE' })
      );
      expect(showNotification).toHaveBeenCalledWith('Queue 8001 deleted', 'success');
    });

    it('does nothing when not confirmed', async () => {
      global.confirm.mockReturnValueOnce(false);

      await deleteQueue('8001');

      expect(global.fetch).not.toHaveBeenCalled();
    });

    it('surfaces API rejection (waiting callers)', async () => {
      global.fetch.mockResolvedValueOnce({
        ok: false,
        json: async () => ({ error: 'Queue 8001 has waiting callers; cannot delete' })
      });

      await deleteQueue('8001');

      expect(showNotification).toHaveBeenCalledWith(
        'Queue 8001 has waiting callers; cannot delete',
        'error'
      );
    });
  });

  describe('removeQueueAgent', () => {
    it('DELETEs the agent from the queue after confirmation', async () => {
      global.fetch.mockResolvedValue({
        ok: true,
        json: async () => ({ success: true, queues: [], agents: [] })
      });

      await removeQueueAgent('8001', '1001');

      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:9000/api/queues/8001/agents/1001',
        expect.objectContaining({ method: 'DELETE' })
      );
      expect(showNotification).toHaveBeenCalledWith(
        'Agent 1001 removed from 8001',
        'success'
      );
    });

    it('does nothing when not confirmed', async () => {
      global.confirm.mockReturnValueOnce(false);

      await removeQueueAgent('8001', '1001');

      expect(global.fetch).not.toHaveBeenCalled();
    });
  });
});
