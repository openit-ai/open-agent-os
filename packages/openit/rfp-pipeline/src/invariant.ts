export const INVARIANT_SCOPE = 'openit-rfp-pipeline'
export function invariant(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(`[${INVARIANT_SCOPE}] invariant failed: ${message}`)
}
