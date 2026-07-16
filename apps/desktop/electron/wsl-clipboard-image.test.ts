import assert from 'node:assert/strict'
import fs from 'node:fs'

import { test } from 'vitest'

import {
  decodeClipboardImageBase64,
  encodePowerShellCommand,
  powershellCandidates,
  readWslWindowsClipboardImage
} from './wsl-clipboard-image'
import { PS_SCRIPT } from './wsl-clipboard-script'

function sourceOf(fileName: string) {
  return fs.readFileSync(new URL(fileName, import.meta.url), 'utf8')
}

// Bitdefender quarantines any single file carrying both the inline PowerShell
// script text and the flag that executes base64-encoded PowerShell, which
// deletes the module from disk and breaks the Electron build. The two halves
// must stay in separate files.
const ENCODED_COMMAND_FLAG = '-EncodedCommand'
const PS_SCRIPT_MARKERS = ['Add-Type -AssemblyName', 'System.Windows.Forms.Clipboard', 'System.IO.MemoryStream']

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

function fakePngBuffer(extraBytes = 16) {
  return Buffer.concat([PNG_SIGNATURE, Buffer.alloc(extraBytes, 0x42)])
}

test('encodePowerShellCommand produces UTF-16LE base64 PowerShell can decode', () => {
  const encoded = encodePowerShellCommand('Write-Output "hi"')
  const roundTripped = Buffer.from(encoded, 'base64').toString('utf16le')
  assert.equal(roundTripped, 'Write-Output "hi"')
})

test('decodeClipboardImageBase64 returns a Buffer for valid PNG base64', () => {
  const png = fakePngBuffer()
  const decoded = decodeClipboardImageBase64(png.toString('base64'))
  assert.ok(Buffer.isBuffer(decoded))
  assert.ok(decoded.equals(png))
})

test('decodeClipboardImageBase64 trims surrounding whitespace before decoding', () => {
  const png = fakePngBuffer()
  const decoded = decodeClipboardImageBase64(`\n  ${png.toString('base64')}  \r\n`)
  assert.ok(decoded && decoded.equals(png))
})

test('decodeClipboardImageBase64 returns null for empty / whitespace input', () => {
  assert.equal(decodeClipboardImageBase64(''), null)
  assert.equal(decodeClipboardImageBase64('   \n  '), null)
  assert.equal(decodeClipboardImageBase64(null), null)
  assert.equal(decodeClipboardImageBase64(undefined), null)
})

test('decodeClipboardImageBase64 rejects base64 without a PNG signature', () => {
  // Valid base64, but the decoded bytes are not a PNG.
  const notPng = Buffer.from('this is not a png at all').toString('base64')
  assert.equal(decodeClipboardImageBase64(notPng), null)
})

test('readWslWindowsClipboardImage decodes the first candidate that returns a PNG', () => {
  const png = fakePngBuffer()
  const calls = []

  const exec = ((cmd, args) => {
    calls.push({ cmd, args })

    return png.toString('base64')
  }) as any

  const result = readWslWindowsClipboardImage({ exec, candidates: ['powershell.exe'] })
  assert.ok(result && result.equals(png))
  assert.equal(calls.length, 1)
  assert.equal(calls[0].cmd, 'powershell.exe')
  // -STA is mandatory for System.Windows.Forms.Clipboard.
  assert.ok(calls[0].args.includes('-STA'))
  assert.ok(calls[0].args.includes('-EncodedCommand'))
})

test('readWslWindowsClipboardImage returns null and stops when stdout is empty (no image)', () => {
  let count = 0

  const exec = (() => {
    count += 1

    return ''
  }) as any

  const result = readWslWindowsClipboardImage({
    exec,
    candidates: ['powershell.exe', '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe']
  })

  assert.equal(result, null)
  // Empty stdout means "no image on the clipboard" — don't probe further candidates.
  assert.equal(count, 1)
})

test('readWslWindowsClipboardImage falls through to the next candidate when one throws', () => {
  const png = fakePngBuffer()
  const seen = []

  const exec = cmd => {
    seen.push(cmd)

    if (cmd === 'powershell.exe') {
      throw Object.assign(new Error('not found'), { code: 'ENOENT' })
    }

    return png.toString('base64') as any
  }

  const result = readWslWindowsClipboardImage({
    exec,
    candidates: ['powershell.exe', '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe']
  })

  assert.ok(result && result.equals(png))
  assert.deepEqual(seen, ['powershell.exe', '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'])
})

test('readWslWindowsClipboardImage returns null when every candidate throws', () => {
  const exec = () => {
    throw new Error('boom')
  }

  const result = readWslWindowsClipboardImage({ exec, candidates: ['a', 'b'] })
  assert.equal(result, null)
})

test('the runner file carries the -EncodedCommand flag but not the PowerShell script text', () => {
  const source = sourceOf('./wsl-clipboard-image.ts')

  assert.ok(source.includes(ENCODED_COMMAND_FLAG))

  for (const marker of PS_SCRIPT_MARKERS) {
    assert.ok(!source.includes(marker), `wsl-clipboard-image.ts must not inline the PowerShell script (found: ${marker})`)
  }
})

test('the script file carries the PowerShell script text but not the -EncodedCommand flag', () => {
  const source = sourceOf('./wsl-clipboard-script.ts')

  for (const marker of PS_SCRIPT_MARKERS) {
    assert.ok(source.includes(marker))
  }

  assert.ok(
    !source.includes(ENCODED_COMMAND_FLAG),
    'wsl-clipboard-script.ts must not carry the encoded-execution flag alongside the script'
  )
})

test('readWslWindowsClipboardImage still sends the intact script through -EncodedCommand', () => {
  const calls = []

  const exec = ((cmd, args) => {
    calls.push({ cmd, args })

    return fakePngBuffer().toString('base64')
  }) as any

  readWslWindowsClipboardImage({ exec, candidates: ['powershell.exe'] })

  const args = calls[0].args
  const encoded = args[args.indexOf(ENCODED_COMMAND_FLAG) + 1]

  // Splitting the script into its own module must not change the bytes that
  // reach PowerShell.
  assert.equal(Buffer.from(encoded, 'base64').toString('utf16le'), PS_SCRIPT)
})

test('powershellCandidates lists the bare name first, then the absolute fallback', () => {
  const candidates = powershellCandidates()
  assert.equal(candidates[0], 'powershell.exe')
  assert.ok(candidates.some(c => c.endsWith('WindowsPowerShell/v1.0/powershell.exe')))
})
