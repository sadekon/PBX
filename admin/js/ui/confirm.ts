/**
 * Confirmation dialog.
 *
 * Replaces `window.confirm()`, which cannot be styled, blocks the whole browser
 * while it is up, and labels itself with the page's origin rather than the
 * application. This is an ordinary modal built from the `.dialog` vocabulary in
 * patterns.css, so it inherits the surface treatment on a glass page and stays
 * legible on one that has not been ported.
 *
 * The element is created once, lazily, and reused -- there is no markup to add
 * to index.html, so any page can call this without wiring anything up.
 *
 * Messages are plain text and are escaped on the way in. Callers cannot pass
 * markup, which is deliberate: the subject of a confirmation is usually an
 * operator-supplied name (an extension, a queue, a trunk), and a template that
 * accepted HTML would make every call site a place to get escaping wrong.
 * `confirmDelete()` builds the one piece of emphasis this needs itself.
 */

import { escapeHtml } from '../utils/html.ts';

export interface ConfirmOptions {
    /** The question. Plain text; escaped before display. */
    message: string;
    /** Label for the confirming button. */
    confirmLabel?: string;
    /** Styles the confirming button as destructive. Default true. */
    danger?: boolean;
}

let element: HTMLElement | null = null;

/**
 * Resolver for the dialog currently up, or null when nothing is waiting.
 *
 * This -- not the `.active` class -- is what "a dialog is open" means here. The
 * shared Escape handler in ui/tabs.ts is registered before this module's and
 * removes `.active` from whatever modal is open, so a handler that gated on the
 * class would find it already gone by the time it ran and would never resolve,
 * leaving the caller awaiting forever.
 */
let resolveCurrent: ((answer: boolean) => void) | null = null;

/** Focus to hand back when the dialog closes. */
let previouslyFocused: HTMLElement | null = null;

function settle(answer: boolean): void {
    const resolve = resolveCurrent;
    resolveCurrent = null;
    element?.classList.remove('active');
    previouslyFocused?.focus();
    previouslyFocused = null;
    if (resolve) resolve(answer);
}

function build(): HTMLElement {
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.id = 'confirm-modal';
    modal.innerHTML = `
        <div class="glass-page">
            <div class="g g1 dialog confirm-dialog" role="alertdialog" aria-modal="true"
                 aria-labelledby="confirm-message">
                <div class="confirm-body">
                    <p id="confirm-message"></p>
                </div>
                <div class="dialog-foot">
                    <span class="spacer"></span>
                    <button type="button" class="btn-ghost" id="confirm-cancel">Cancel</button>
                    <button type="button" class="btn-ghost" id="confirm-accept"></button>
                </div>
            </div>
        </div>`;
    document.body.appendChild(modal);

    modal.querySelector('#confirm-cancel')?.addEventListener('click', () => settle(false));
    modal.querySelector('#confirm-accept')?.addEventListener('click', () => settle(true));

    // The scrim dismisses; anywhere inside the dialog does not.
    modal.addEventListener('click', (event: Event) => {
        if (event.target === modal) settle(false);
    });

    document.addEventListener('keydown', (event: KeyboardEvent) => {
        if (event.key === 'Escape' && resolveCurrent) settle(false);
    });

    return modal;
}

/**
 * Asks the operator to confirm something, resolving true if they do.
 *
 * Dismissing -- Escape, the scrim, or Cancel -- resolves false rather than
 * rejecting, so a call site reads as a plain condition and no path needs a
 * catch to avoid an unhandled rejection.
 */
export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
    return present(escapeHtml(options.message), options.confirmLabel ?? 'Confirm',
        options.danger !== false);
}

/** Shared by the escaped-text and pre-built-markup entry points. */
function present(bodyHtml: string, confirmLabel: string, danger: boolean): Promise<boolean> {
    // A second call while one is up answers the first rather than stranding it.
    if (resolveCurrent) settle(false);

    element = element ?? build();
    previouslyFocused = document.activeElement as HTMLElement | null;

    const message = element.querySelector('#confirm-message');
    if (message) message.innerHTML = bodyHtml;

    const accept = element.querySelector('#confirm-accept') as HTMLElement | null;
    if (accept) {
        accept.textContent = confirmLabel;
        accept.classList.toggle('btn-ghost-danger', danger);
        accept.classList.toggle('btn-ghost-accent', !danger);
    }

    element.classList.add('active');

    // Cancel takes focus, not the confirming button: Enter pressed on a dialog
    // that has not been read should do nothing.
    (element.querySelector('#confirm-cancel') as HTMLElement | null)?.focus();

    return new Promise<boolean>((resolve) => { resolveCurrent = resolve; });
}

/**
 * "Are you sure you'd like to delete <subject>?"
 *
 * The subject is named rather than left as "this", so the sentence says which
 * of five rows on screen is about to go.
 */
export function confirmDelete(subject: string): Promise<boolean> {
    const named = subject.trim();
    const body = named
        ? `Are you sure you'd like to delete <strong>${escapeHtml(named)}</strong>? This cannot be undone.`
        : 'Are you sure you\'d like to delete this? This cannot be undone.';
    return present(body, 'Delete', true);
}
