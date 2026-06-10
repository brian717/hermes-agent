import { describe, expect, test } from 'vitest'

import { envFlag, envOutputLines, envOutputLinesSet, TOOL_OUTPUT_LINES_DEFAULT } from '../logic/env.ts'

describe('envFlag', () => {
  test('recognizes truthy values regardless of case/whitespace', () => {
    for (const v of ['1', 'true', 'yes', 'on', 'TRUE', 'Yes', ' on ']) {
      expect(envFlag(v, false)).toBe(true)
    }
  })

  test('recognizes falsy values regardless of case/whitespace', () => {
    for (const v of ['0', 'false', 'no', 'off', 'FALSE', 'No', ' off ']) {
      expect(envFlag(v, true)).toBe(false)
    }
  })

  test('returns fallback when unset', () => {
    expect(envFlag(undefined, true)).toBe(true)
    expect(envFlag(undefined, false)).toBe(false)
    expect(envFlag('', true)).toBe(true)
    expect(envFlag('   ', false)).toBe(false)
  })

  test('returns fallback for unrecognized garbage', () => {
    expect(envFlag('maybe', true)).toBe(true)
    expect(envFlag('maybe', false)).toBe(false)
    expect(envFlag('2', true)).toBe(true)
    expect(envFlag('enabled', false)).toBe(false)
  })
})

describe('envOutputLines (HERMES_TUI_TOOL_OUTPUT_LINES)', () => {
  test('unset → the 200-line default (today’s behavior)', () => {
    expect(TOOL_OUTPUT_LINES_DEFAULT).toBe(200)
    expect(envOutputLines(undefined)).toBe(200)
    expect(envOutputLines('')).toBe(200)
    expect(envOutputLines('   ')).toBe(200)
  })

  test('a positive integer → that cap (whitespace-tolerant)', () => {
    expect(envOutputLines('50')).toBe(50)
    expect(envOutputLines(' 50 ')).toBe(50)
    expect(envOutputLines('1')).toBe(1)
    expect(envOutputLines('1000')).toBe(1000)
  })

  test('"0" → Infinity (UNLIMITED — show the entire output)', () => {
    expect(envOutputLines('0')).toBe(Number.POSITIVE_INFINITY)
  })

  test('garbage → the 200-line default', () => {
    expect(envOutputLines('unlimited')).toBe(200)
    expect(envOutputLines('-5')).toBe(200)
    expect(envOutputLines('1.5')).toBe(200)
    expect(envOutputLines('50 lines')).toBe(200)
  })

  test('envOutputLinesSet: set means any non-empty value, even garbage', () => {
    expect(envOutputLinesSet(undefined)).toBe(false)
    expect(envOutputLinesSet('')).toBe(false)
    expect(envOutputLinesSet('   ')).toBe(false)
    expect(envOutputLinesSet('0')).toBe(true)
    expect(envOutputLinesSet('50')).toBe(true)
    expect(envOutputLinesSet('garbage')).toBe(true)
  })
})
