/**
 * framework-directives.ts — Layer 7 of the prompt-assembly onion (Story 15.13).
 *
 * Mode-keyed framework directives. Only `deck` is implemented today (PPTX-
 * compatible HTML slides); other modes (`prototype`, `blueprint`, `report`)
 * intentionally have no entry — meaning composeSystemPrompt() will simply
 * skip the framework layer for them. To add a new framework, append a key
 * to FRAMEWORK_DIRECTIVES; getFrameworkDirective() returns '' for unknown
 * modes so the layer is silently omitted.
 */

export const FRAMEWORK_DIRECTIVES: Record<string, string> = {
  deck: `## FRAMEWORK: PPTX-COMPATIBLE DECK

- Output one HTML file per slide, separated by <!--SLIDE--> markers.
- Each slide MUST fit a 16:9 viewport (1280x720) without scroll.
- Use only HTML + inline CSS; no external assets, no JS.
- Slide 1 is the title slide; the last slide is a CTA / Q&A.
- Maximum 7 bullets per slide; maximum 14 words per bullet.`,
  // 未来扩展：report / blueprint / prototype 等 mode 加各自 directive
};

/**
 * Returns the framework directive string for a given skill mode, or empty
 * string when the mode has no entry (so the layer is dropped cleanly).
 */
export function getFrameworkDirective(mode: string | undefined | null): string {
  if (!mode) return '';
  return FRAMEWORK_DIRECTIVES[mode] ?? '';
}
