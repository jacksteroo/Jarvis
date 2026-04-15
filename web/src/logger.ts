const PREFIX = '[PepperWeb]'

let traceCounter = 0

function timestamp() {
  return new Date().toISOString()
}

export function nextTraceId(prefix = 'trace') {
  traceCounter += 1
  return `${prefix}-${Date.now()}-${traceCounter}`
}

export function previewText(value: string, max = 160) {
  const normalized = value.replace(/\s+/g, ' ').trim()
  if (normalized.length <= max) {
    return normalized
  }

  return `${normalized.slice(0, max)}...`
}

function format(scope: string, event: string) {
  return `${PREFIX} ${timestamp()} [${scope}] ${event}`
}

export function logInfo(scope: string, event: string, details?: Record<string, unknown>) {
  if (details) {
    console.log(format(scope, event), details)
    return
  }

  console.log(format(scope, event))
}

export function logWarn(scope: string, event: string, details?: Record<string, unknown>) {
  if (details) {
    console.warn(format(scope, event), details)
    return
  }

  console.warn(format(scope, event))
}

export function logError(scope: string, event: string, details?: Record<string, unknown>) {
  if (details) {
    console.error(format(scope, event), details)
    return
  }

  console.error(format(scope, event))
}
