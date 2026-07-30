/**
 * Inline-SVG icon set for the admin UI.
 *
 * Monochrome, 24×24 viewBox, `currentColor` stroke — a small shared basis for
 * replacing decorative emoji with a consistent icon language. Icons are derived
 * from Lucide (ISC license). Add new glyphs here rather than inlining raw SVG
 * at call sites so the set stays consistent and reusable.
 */

const ICON_PATHS: Record<string, string> = {
  'log-in':
    '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>' +
    '<polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>',
  'log-out':
    '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>' +
    '<polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  play: '<polygon points="6 3 20 12 6 21 6 3"/>',
  pencil:
    '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352' +
    'a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>',
  'user-plus':
    '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>' +
    '<line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/>',
  trash:
    '<polyline points="3 6 5 6 21 6"/>' +
    '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>' +
    '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
};

// Glyphs drawn as solid shapes rather than strokes.
const FILLED_ICONS = new Set(['play']);

/**
 * Return inline `<svg>` markup for a named icon, sized via the `.icon` class
 * (see admin.css) and inheriting the current text color.
 */
export function icon(name: keyof typeof ICON_PATHS | string): string {
  const path = ICON_PATHS[name] ?? '';
  const fill = FILLED_ICONS.has(name) ? 'currentColor' : 'none';
  return (
    `<svg class="icon" viewBox="0 0 24 24" fill="${fill}" stroke="currentColor" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`
  );
}
