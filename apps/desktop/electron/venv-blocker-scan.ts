'use strict'

/**
 * venv-blocker-scan.ts
 *
 * Thin helper that runs the Python venv-blocker scan as a subprocess and
 * returns a typed result for the Desktop update preflight.
 */

import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface VenvBlockerProcess {
  pid: number
  name: string
  cmdline: string
  /**
   * False when the blocker is a third-party process that merely runs this
   * install's interpreter (a hand-started `python.exe -m http.server` under
   * the install dir).  It still blocks the update, but "close other Hermes
   * windows" is unactionable advice for it.  Defaults to true when the
   * scanner does not report the field — an older venv scanner during a
   * partial update must not turn every blocker into a "not Hermes" claim.
   */
  hermesOwned: boolean
}

export interface VenvBlockerScanResult {
  blocked: boolean
  processes: VenvBlockerProcess[]
}

export type ScanOutcome =
  | { kind: 'clear'; result: VenvBlockerScanResult }
  | { kind: 'blocked'; result: VenvBlockerScanResult }
  | { kind: 'probe-failure'; error: string }

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SCAN_TIMEOUT_MS = 15000
const SCAN_MODULE = 'hermes_cli._scan_venv_blockers'

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Strictly validate and parse the JSON output from the venv-blocker scan.
 * Pure function — no side effects.
 */
export function parseVenvBlockerScanOutput(raw: string): ScanOutcome {
  let parsed: any

  try {
    parsed = JSON.parse(raw)
  } catch {
    return { kind: 'probe-failure', error: 'malformed JSON' }
  }

  if (!parsed || typeof parsed !== 'object' || parsed.ok !== true) {
    return { kind: 'probe-failure', error: 'missing or invalid ok field' }
  }

  if (typeof parsed.blocked !== 'boolean') {
    return { kind: 'probe-failure', error: 'blocked must be a boolean' }
  }

  if (!Array.isArray(parsed.processes)) {
    return { kind: 'probe-failure', error: 'processes must be an array' }
  }

  const processes: VenvBlockerProcess[] = []

  for (const entry of parsed.processes) {
    if (!entry || typeof entry !== 'object') {
      return { kind: 'probe-failure', error: 'process entry must be an object' }
    }

    const { pid, name, cmdline, hermes_owned: hermesOwned } = entry

    if (!Number.isInteger(pid) || pid <= 0) {
      return { kind: 'probe-failure', error: 'process pid must be a positive integer' }
    }

    if (typeof name !== 'string' || name.length === 0) {
      return { kind: 'probe-failure', error: 'process name must be a non-empty string' }
    }

    if (typeof cmdline !== 'string') {
      return { kind: 'probe-failure', error: 'process cmdline must be a string' }
    }

    if (hermesOwned !== undefined && typeof hermesOwned !== 'boolean') {
      return { kind: 'probe-failure', error: 'process hermes_owned must be a boolean' }
    }

    processes.push({ pid, name, cmdline, hermesOwned: hermesOwned !== false })
  }

  // Reject inconsistent combinations
  if (parsed.blocked && processes.length === 0) {
    return { kind: 'probe-failure', error: 'blocked is true but process list is empty' }
  }

  if (!parsed.blocked && processes.length > 0) {
    return { kind: 'probe-failure', error: 'blocked is false but process list is non-empty' }
  }

  return parsed.blocked
    ? { kind: 'blocked', result: { blocked: true, processes } }
    : { kind: 'clear', result: { blocked: false, processes } }
}

/**
 * Run the venv-blocker scan subprocess.  Async so the Electron main-process
 * event loop is never blocked by the psutil process scan (up to 15s on a
 * loaded Windows box).  Accepts optional overrides for testing (dependency
 * injection).
 */
export async function scanVenvBlockers(
  updateRoot: string,
  execOverride?: typeof execFileAsync,
  resolveOverride?: typeof resolveVenvPython
): Promise<ScanOutcome> {
  const execFn = execOverride || execFileAsync
  const resolveFn = resolveOverride || resolveVenvPython
  const venvPython = resolveFn(updateRoot)

  if (!venvPython) {
    return { kind: 'probe-failure', error: 'venv python not found' }
  }

  let stdout: string

  try {
    const proc = await execFn(venvPython, ['-m', SCAN_MODULE], {
      cwd: updateRoot,
      encoding: 'utf-8',
      timeout: SCAN_TIMEOUT_MS,
      windowsHide: true
    } as any)

    stdout = String((proc as any).stdout ?? '')
  } catch (err: any) {
    const diag = [`exit code ${err.status ?? err.code ?? -1}`]

    if (err.stderr) {
      diag.push(String(err.stderr).slice(0, 200))
    }

    return { kind: 'probe-failure', error: diag.join('; ') }
  }

  return parseVenvBlockerScanOutput(stdout)
}

// ---------------------------------------------------------------------------
// Internal helpers (exported for testing)
// ---------------------------------------------------------------------------

/** Resolve the venv python path.  Returns null if the file does not exist. */
export function resolveVenvPython(updateRoot: string): string | null {
  const isWindows = process.platform === 'win32'
  const pythonName = isWindows ? 'python.exe' : 'python3'
  const scriptsDir = isWindows ? 'Scripts' : 'bin'
  const candidate = path.join(updateRoot, 'venv', scriptsDir, pythonName)

  try {
    fs.accessSync(candidate)

    return candidate
  } catch {
    return null
  }
}

/**
 * Build a human-readable error message from blocker scan results.
 * Does NOT recommend --force-venv.
 */
export function formatBlockerMessage(result: VenvBlockerScanResult): string {
  const foreign = result.processes.filter((proc) => !proc.hermesOwned)

  const header =
    foreign.length > 0
      ? 'Update aborted: another process is using this installation.'
      : 'Update aborted: another Hermes process is using this installation.'

  const lines = [header, '', 'These processes must be stopped before updating:', '']

  for (const proc of result.processes.slice(0, 10)) {
    const tag = proc.hermesOwned ? '' : '  <- not a Hermes process'

    lines.push(`  PID ${proc.pid}  ${proc.name}  ${proc.cmdline}${tag}`)
  }

  if (result.processes.length > 10) {
    lines.push(`  ... and ${result.processes.length - 10} more`)
  }

  lines.push('')

  if (foreign.length > 0) {
    // Naming the foreign process is the whole point: telling the user to
    // close Hermes windows they do not have is a dead end through the UI.
    lines.push(
      'The processes marked above are not Hermes — they are yours, running ' +
        "from this install's Python.  Stop them by PID."
    )
  }

  lines.push(
    'Close the terminal, app, or service owning that process.  If it is a ' +
      'remote backend, stopping it will disconnect remote clients.'
  )
  lines.push('Then retry the update.')

  return lines.join('\n')
}

/**
 * Build a probe-failure error message.
 */
export function formatProbeFailedMessage(): string {
  return (
    'Update aborted: Desktop could not verify the Hermes installation is free.\n' +
    '\n' +
    'Close other Hermes windows and terminals, then retry.  If the problem\n' +
    'persists, run `hermes update` in a terminal for detailed diagnostics.'
  )
}
