export const INVARIANT_SCOPE = 'openit-business-sales'
export function invariant(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(`[${INVARIANT_SCOPE}] ${message}`)
}
