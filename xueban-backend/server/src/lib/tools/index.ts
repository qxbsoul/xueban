/**
 * tools/index.ts — Module barrel for tool schemas.
 *
 * Post-Phase 2: only the SCHEMAS are re-exported. The executor dispatch table
 * (`skillAnchorExecutors`) was removed alongside the tool_use orchestration
 * it powered — see `skill-anchors.ts` header for the rationale.
 */

export {
  skillAnchorTools,
  type ToolExecutionResult,
  type ToolExecutor,
} from './skill-anchors';
