/**
 * @jest-environment jsdom
 */

import { describe, it, expect, beforeEach } from '@jest/globals';

let confirmDialog;
let confirmDelete;

const modal = () => document.getElementById('confirm-modal');
const message = () => document.getElementById('confirm-message');
const accept = () => document.getElementById('confirm-accept');
const cancel = () => document.getElementById('confirm-cancel');
const click = (el) => el.dispatchEvent(new MouseEvent('click', { bubbles: true }));

describe('Confirmation dialog', () => {
  beforeEach(async () => {
    jest.resetModules();
    document.body.innerHTML = '';
    ({ confirmDialog, confirmDelete } = await import('../js/ui/confirm.ts'));
  });

  it('builds itself, so no page needs markup for it', async () => {
    expect(modal()).toBeNull();
    const answer = confirmDelete('extension 1001');
    expect(modal()).not.toBeNull();
    expect(modal().classList.contains('active')).toBe(true);
    click(cancel());
    await answer;
  });

  it('resolves true only when the confirming button is pressed', async () => {
    const yes = confirmDelete('extension 1001');
    click(accept());
    expect(await yes).toBe(true);

    const no = confirmDelete('extension 1001');
    click(cancel());
    expect(await no).toBe(false);
  });

  it('names the subject in the sentence', async () => {
    const answer = confirmDelete('extension 1001');
    expect(message().textContent).toBe(
      "Are you sure you'd like to delete extension 1001? This cannot be undone.");
    expect(message().querySelector('strong').textContent).toBe('extension 1001');
    click(cancel());
    await answer;
  });

  it('falls back to "this" when there is no subject', async () => {
    const answer = confirmDelete('   ');
    expect(message().textContent).toBe(
      "Are you sure you'd like to delete this? This cannot be undone.");
    click(cancel());
    await answer;
  });

  it('escapes a subject rather than letting it become markup', async () => {
    const answer = confirmDelete('<img src=x onerror=alert(1)>');
    expect(message().querySelector('img')).toBeNull();
    expect(message().textContent).toContain('<img src=x onerror=alert(1)>');
    click(cancel());
    await answer;
  });

  it('escapes a plain message too', async () => {
    const answer = confirmDialog({ message: 'Reboot <b>all</b> phones?' });
    expect(message().querySelector('b')).toBeNull();
    expect(message().textContent).toBe('Reboot <b>all</b> phones?');
    click(cancel());
    await answer;
  });

  it('focuses Cancel, so Enter on an unread dialog does nothing destructive', async () => {
    const answer = confirmDelete('extension 1001');
    expect(document.activeElement).toBe(cancel());
    click(cancel());
    await answer;
  });

  it('dismisses on the scrim but not on the dialog itself', async () => {
    const answer = confirmDelete('extension 1001');
    click(document.querySelector('.confirm-dialog'));
    expect(modal().classList.contains('active')).toBe(true);
    modal().dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(await answer).toBe(false);
  });

  it('resolves on Escape', async () => {
    const answer = confirmDelete('extension 1001');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(await answer).toBe(false);
  });

  // ui/tabs.ts registers a document-level Escape handler before this module's
  // and strips `.active` from whatever modal is open. A handler that gated on
  // the class would find it already gone and never resolve, leaving the caller
  // awaiting forever. State lives in the resolver, not the class.
  it('still resolves on Escape when another handler removed .active first', async () => {
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        document.querySelector('.modal.active')?.classList.remove('active');
      }
    });
    const answer = confirmDelete('extension 1001');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(await answer).toBe(false);
  });

  it('answers an outstanding dialog rather than stranding it when reopened', async () => {
    const first = confirmDelete('extension 1001');
    const second = confirmDelete('extension 1002');
    expect(await first).toBe(false);
    expect(message().textContent).toContain('extension 1002');
    click(accept());
    expect(await second).toBe(true);
  });

  it('restores focus to whatever opened it', async () => {
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();

    const answer = confirmDelete('extension 1001');
    expect(document.activeElement).toBe(cancel());
    click(cancel());
    await answer;
    expect(document.activeElement).toBe(trigger);
  });

  it('labels the confirming button and styles it by intent', async () => {
    const destructive = confirmDelete('extension 1001');
    expect(accept().textContent).toBe('Delete');
    expect(accept().classList.contains('btn-ghost-danger')).toBe(true);
    click(cancel());
    await destructive;

    const benign = confirmDialog({ message: 'Reboot all phones?', confirmLabel: 'Reboot', danger: false });
    expect(accept().textContent).toBe('Reboot');
    expect(accept().classList.contains('btn-ghost-danger')).toBe(false);
    expect(accept().classList.contains('btn-ghost-accent')).toBe(true);
    click(cancel());
    await benign;
  });
});
