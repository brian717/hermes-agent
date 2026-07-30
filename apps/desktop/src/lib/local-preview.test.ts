import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import { browserPreviewUrl } from './local-preview'

const saveImageBuffer = vi.fn(async () => 'C:\\Users\\me\\AppData\\Local\\Temp\\hermes\\report.html')

const api = vi.fn(async ({ path }: { path: string }) => {
  if (path.startsWith('/api/fs/read-data-url?')) {
    // "<h1>hi</h1>"
    return { dataUrl: 'data:text/html;base64,PGgxPmhpPC9oMT4=' }
  }

  throw new Error(`unexpected path ${path}`)
})

function fileTarget(path: string) {
  return {
    kind: 'file' as const,
    label: 'report.html',
    path,
    previewKind: 'html' as const,
    source: path,
    url: `file://${path}`
  }
}

beforeEach(() => {
  vi.stubGlobal('window', { hermesDesktop: { api, saveImageBuffer } })
})

afterEach(() => {
  $connection.set(null)
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('browserPreviewUrl', () => {
  it('hands local targets straight to the OS', async () => {
    $connection.set({ mode: 'local' } as never)

    expect(await browserPreviewUrl(fileTarget('/Users/me/report.html'))).toBe('file:///Users/me/report.html')
    expect(api).not.toHaveBeenCalled()
    expect(saveImageBuffer).not.toHaveBeenCalled()
  })

  it('stages a backend file locally in remote mode instead of passing the remote path', async () => {
    $connection.set({ mode: 'remote', remoteKind: 'ssh', remoteHost: 'box' } as never)

    const url = await browserPreviewUrl(fileTarget('/home/me/report.html'))

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/api/fs/read-data-url?path=%2Fhome%2Fme%2Freport.html' })
    )

    const [bytes, ext] = saveImageBuffer.mock.calls[0] as unknown as [Uint8Array, string]

    expect(new TextDecoder().decode(bytes)).toBe('<h1>hi</h1>')
    expect(ext).toBe('.html')
    // The client OS never sees /home/me/report.html — that path is on the backend.
    expect(url).toBe('file:///C:/Users/me/AppData/Local/Temp/hermes/report.html')
  })

  it('leaves http targets alone in remote mode', async () => {
    $connection.set({ mode: 'remote', remoteKind: 'ssh', remoteHost: 'box' } as never)

    const url = await browserPreviewUrl({
      kind: 'url',
      label: 'example',
      source: 'https://example.com',
      url: 'https://example.com'
    })

    expect(url).toBe('https://example.com')
    expect(saveImageBuffer).not.toHaveBeenCalled()
  })

  it('reports a failure rather than opening a path that is not there', async () => {
    $connection.set({ mode: 'remote', remoteKind: 'ssh', remoteHost: 'box' } as never)
    saveImageBuffer.mockResolvedValueOnce('')

    await expect(browserPreviewUrl(fileTarget('/home/me/report.html'))).rejects.toThrow(/stage/i)
  })
})
