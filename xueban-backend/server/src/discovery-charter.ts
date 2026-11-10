/**
 * discovery-charter.ts — Layer 1 of the prompt-assembly onion (Story 15.13).
 *
 * DISCOVERY directives forcing the LLM to restate intent, list a plan, and
 * surface ambiguities BEFORE producing any artifact. Wrapped in <sf:discovery>
 * so the parser / front-end can render the discovery block as an inspector
 * step (existing parser already handles <sf:*> tags).
 *
 * The directive instructs the model to emit the discovery block ONCE per
 * session (first reply), so multi-turn sessions don't keep re-listing plans.
 */

export const DISCOVERY_CHARTER = `## DISCOVERY MODE

Before producing any artifact, you MUST:

1. Restate the user's intent in one sentence (max 25 words).
2. List a TodoWrite-style plan: 3-7 bullet points of concrete steps.
3. Identify ambiguities: list at most 2 questions you would ask if you could.
   If none, write "No ambiguities."
4. Then proceed.

Output the discovery block once, at the very start of your first reply, wrapped in:

<sf:discovery>
intent: ...
plan:
  - ...
ambiguities: ...
</sf:discovery>

Do not repeat the discovery block in subsequent replies of the same session.`;
