export function resolveUserLevel(ctx: unknown): number {
  try {
    const anyCtx = ctx as Record<string, unknown>
    let perm: unknown = anyCtx['permission']
    if (!perm && typeof anyCtx['get'] === 'function') {
      try { perm = (anyCtx['get'] as (k: string) => unknown)('permission') } catch { perm = undefined }
    }
    if (perm && typeof perm === 'object') {
      const p = perm as Record<string, unknown>
      if (typeof p['getLevel'] === 'function') return (p['getLevel'] as () => number)()
      if (typeof p['level'] === 'number') return p['level'] as number
      if (typeof p['currentLevel'] === 'number') return p['currentLevel'] as number
    }
  } catch {
    // fall through
  }
  if (typeof console !== 'undefined' && typeof console.warn === 'function') {
    console.warn('[openit-business-board] permission service not found, fallback L2')
  }
  return 2
}

export function filterByLevel<T extends { level: number }>(items: readonly T[], userLevel: number): T[] {
  // L0 sees all: 0 <= any => true
  return items.filter((it) => userLevel <= it.level)
}
