/**
 * Cordis plugin "openit-rfp-pipeline" — service ctx.rfpPipeline
 */
import { Context, Service } from '@deepseek-ai/cordis'

export const name = 'openit-rfp-pipeline'

declare module '@deepseek-ai/cordis' {
  interface Context {
    rfpPipeline: RfpPipelineService
  }
  interface Events {
    'openit-rfp-pipeline/ready'(): void
  }
}

export class RfpPipelineService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'rfpPipeline')
  }
  get status(): string {
    return 'openit-rfp-pipeline ready'
  }
}

export function apply(ctx: Context): void {
  ctx.provide('rfpPipeline', new RfpPipelineService(ctx) as any)
  ctx.emit('openit-rfp-pipeline/ready' as any)
}
