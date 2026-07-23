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
  loadQueues,
  loadQueueAgents,
  setQueueAgentState,
  deleteQueue
} from '../js/pages/queues.ts';
import { showNotification } from '../js/ui/notifications.ts';

global.fetch = jest.fn();
global.confirm = jest.fn(() => true);

document.body.innerHTML = `
  <table><tbody id="queues-table-body"></tbody></table>
  <table><tbody id="queue-agents-table-body"></tbody></table>
`;

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

describe('Call Queues page', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    global.confirm.mockReturnValue(true);
    document.getElementById('queues-table-body').innerHTML = '';
    document.getElementById('queue-agents-table-body').innerHTML = '';
  });

  describe('loadQueues', () => {
    it('renders queue rows', async () => {
      global.fetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ queues: [QUEUE] })
      });

      await loadQueues();

      const html = document.getElementById('queues-table-body').innerHTML;
      expect(html).toContain('8001');
      expect(html).toContain('Sales');
      expect(html).toContain('round_robin');
      expect(html).toContain('1/2');
      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:9000/api/queues',
        expect.objectContaining({ headers: expect.any(Object) })
      );
    });

    it('renders empty state', async () => {
      global.fetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ queues: [] })
      });

      await loadQueues();

      expect(document.getElementById('queues-table-body').innerHTML)
        .toContain('No call queues configured');
    });

    it('leaves table untouched on HTTP error', async () => {
      global.fetch.mockResolvedValueOnce({ ok: false, status: 500 });

      await loadQueues();

      expect(document.getElementById('queues-table-body').innerHTML).toBe('');
    });
  });

  describe('loadQueueAgents', () => {
    it('renders agent rows with status', async () => {
      global.fetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ agents: [AGENT] })
      });

      await loadQueueAgents();

      const html = document.getElementById('queue-agents-table-body').innerHTML;
      expect(html).toContain('1001');
      expect(html).toContain('Available');
      expect(html).toContain('Log out');
    });

    it('shows paused status with reason', async () => {
      global.fetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          agents: [{ ...AGENT, paused: true, pause_reason: 'auto_missed' }]
        })
      });

      await loadQueueAgents();

      expect(document.getElementById('queue-agents-table-body').innerHTML)
        .toContain('Paused (auto_missed)');
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
});
