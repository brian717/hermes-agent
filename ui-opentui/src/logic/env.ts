/**
 * env — shared boolean env-flag parsing (one source for the TRUE/FALSE regexes).
 *
 * Recognized truthy values: 1/true/yes/on; falsy: 0/false/no/off (case-insensitive,
 * surrounding whitespace trimmed). Anything else (incl. unset) is "unrecognized".
 */
export const TRUE_RE = /^(?:1|true|yes|on)$/i
export const FALSE_RE = /^(?:0|false|no|off)$/i

/** Parse a boolean env var; returns `fallback` when unset/unrecognized. */
export function envFlag(value: string | undefined, fallback: boolean): boolean {
  const v = value?.trim() ?? ''
  if (TRUE_RE.test(v)) return true
  if (FALSE_RE.test(v)) return false
  return fallback
}

/** Default cap on output lines shown by an EXPANDED tool body. */
export const TOOL_OUTPUT_LINES_DEFAULT = 200

/**
 * Parse `HERMES_TUI_TOOL_OUTPUT_LINES` (a TUI-only env var — deliberately NOT
 * a config.yaml knob): how many output lines an expanded tool body shows.
 * Unset/garbage → 200 (the long-standing default); a positive integer → that
 * cap; `0` → Infinity (UNLIMITED — show the entire output).
 */
export function envOutputLines(value: string | undefined): number {
  const v = value?.trim() ?? ''
  if (!/^\d+$/.test(v)) return TOOL_OUTPUT_LINES_DEFAULT
  const n = Number.parseInt(v, 10)
  return n === 0 ? Number.POSITIVE_INFINITY : n
}

/**
 * Whether `HERMES_TUI_TOOL_OUTPUT_LINES` was EXPLICITLY set (any non-empty
 * value, even an unparseable one). When it is, the store prefers the
 * always-full raw `result` over a gateway tail-capped `result_text` — see
 * store.ts tool.complete.
 */
export function envOutputLinesSet(value: string | undefined): boolean {
  return (value?.trim() ?? '') !== ''
}
