import { isDesktopFsRemoteMode, readDesktopFileDataUrl, readDesktopFileText } from '@/lib/desktop-fs'
import type { PreviewTarget } from '@/store/preview'

const HTML_EXTENSIONS = new Set(['.htm', '.html'])
const IMAGE_EXTENSIONS = new Set(['.bmp', '.gif', '.jpeg', '.jpg', '.png', '.svg', '.webp'])

const LANGUAGE_BY_EXT: Record<string, string> = {
  '.c': 'c',
  '.conf': 'ini',
  '.cpp': 'cpp',
  '.css': 'css',
  '.csv': 'csv',
  '.go': 'go',
  '.graphql': 'graphql',
  '.h': 'c',
  '.hpp': 'cpp',
  '.html': 'html',
  '.java': 'java',
  '.js': 'javascript',
  '.json': 'json',
  '.jsx': 'jsx',
  '.log': 'text',
  '.lua': 'lua',
  '.md': 'markdown',
  '.mjs': 'javascript',
  '.py': 'python',
  '.rb': 'ruby',
  '.rs': 'rust',
  '.sh': 'shell',
  '.sql': 'sql',
  '.svg': 'xml',
  '.toml': 'toml',
  '.ts': 'typescript',
  '.tsx': 'tsx',
  '.txt': 'text',
  '.xml': 'xml',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.zsh': 'shell'
}

function basename(value: string) {
  return value.split(/[\\/]/).filter(Boolean).pop() || value
}

function extension(value: string) {
  const clean = value.split(/[?#]/, 1)[0] || value
  const idx = clean.lastIndexOf('.')

  return idx >= 0 ? clean.slice(idx).toLowerCase() : ''
}

function joinPath(base: string, rel: string) {
  if (!base) {
    return rel
  }

  return `${base.replace(/\/+$/, '')}/${rel.replace(/^\.?\//, '')}`
}

function pathToFileUrl(path: string) {
  const encoded = path
    .split('/')
    .map(part => encodeURIComponent(part))
    .join('/')

  return `file://${encoded.startsWith('/') ? encoded : `/${encoded}`}`
}

export function localPreviewTarget(rawTarget: string, cwd?: string | null): PreviewTarget | null {
  const raw = rawTarget.trim().replace(/^`|`$/g, '')

  if (!raw) {
    return null
  }

  if (/^https?:\/\//i.test(raw)) {
    return { kind: 'url', label: basename(raw), source: raw, url: raw }
  }

  let path = raw

  if (/^file:\/\//i.test(raw)) {
    try {
      path = decodeURIComponent(new URL(raw).pathname)
    } catch {
      path = raw.replace(/^file:\/\//i, '')
    }
  } else if (!raw.startsWith('/') && cwd) {
    path = joinPath(cwd, raw)
  }

  const ext = extension(path)
  const isHtml = HTML_EXTENSIONS.has(ext)
  const isImage = IMAGE_EXTENSIONS.has(ext)

  return {
    kind: 'file',
    label: basename(path),
    language: LANGUAGE_BY_EXT[ext] || 'text',
    path,
    // Renderer fallback can't stat/sniff without reading; assume text unless
    // image/html extension says otherwise. LocalFilePreview still guards
    // binary/large files when readFileText/readFileDataUrl returns metadata.
    previewKind: isHtml ? 'html' : isImage ? 'image' : 'text',
    source: raw,
    url: pathToFileUrl(path)
  }
}

async function enrichPreviewTarget(target: PreviewTarget | null): Promise<PreviewTarget | null> {
  if (!isDesktopFsRemoteMode() || !target || target.kind !== 'file' || target.previewKind === 'image') {
    return target
  }

  try {
    const result = await readDesktopFileText(target.path || target.source)

    return {
      ...target,
      binary: result.binary,
      byteSize: result.byteSize,
      language: result.language || target.language,
      large: (result.byteSize ?? 0) > 512 * 1024,
      mimeType: result.mimeType
    }
  } catch {
    return target
  }
}

function base64ToBytes(base64: string) {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)

  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i)
  }

  return bytes
}

/** A path the client OS can actually resolve. Mirrors the artifact pane's
 *  `openHtmlInBrowser`: a saved temp file, not the renderer's encoded form. */
function clientPathToFileUrl(path: string) {
  const normalized = path.replace(/\\/g, '/')

  return `file://${normalized.startsWith('/') ? '' : '/'}${normalized}`
}

/**
 * URL to hand the client's OS for "open in browser".
 *
 * In remote (SSH) mode a file target's `url` is a `file://` URL built from a
 * path on the *backend* host, so `shell.openExternal` on the client resolves
 * nothing and Electron reports "No application found to open URL" (#75011).
 * Fetch the bytes through the FS API and stage them in a client-side temp file
 * first — the same trick the artifact pane uses for generated HTML. Relative
 * assets are not fetched, so a page that pulls in siblings renders bare; that
 * still beats a dead-end toast.
 */
export async function browserPreviewUrl(target: PreviewTarget): Promise<string> {
  if (!isDesktopFsRemoteMode() || target.kind !== 'file') {
    return target.url
  }

  const path = target.path || target.source
  const dataUrl = await readDesktopFileDataUrl(path)
  const separator = dataUrl.indexOf(',')

  if (separator < 0 || !/;base64$/i.test(dataUrl.slice(0, separator))) {
    throw new Error('Could not read the file from the remote backend')
  }

  const saved = await window.hermesDesktop?.saveImageBuffer?.(
    base64ToBytes(dataUrl.slice(separator + 1)),
    extension(path) || '.txt'
  )

  if (!saved) {
    throw new Error('Could not stage the remote file for preview')
  }

  return clientPathToFileUrl(saved)
}

export async function normalizeOrLocalPreviewTarget(
  rawTarget: string,
  cwd?: string | null
): Promise<PreviewTarget | null> {
  try {
    const normalized = await window.hermesDesktop?.normalizePreviewTarget?.(rawTarget, cwd || undefined)

    if (normalized) {
      return enrichPreviewTarget(normalized)
    }
  } catch {
    // Running Electron may still have the old HTML-only preview IPC. Fall
    // through to renderer-side local classification so text/images still open.
  }

  return enrichPreviewTarget(localPreviewTarget(rawTarget, cwd))
}
