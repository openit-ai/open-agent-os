/**
 * Cordis plugin "openit-hermes" — service ctx.hermes
 */
import { Context, Service } from '@deepseek-ai/cordis'

export const name = 'openit-hermes'

declare module '@deepseek-ai/cordis' {
  interface Context {
    hermes: HermesService
  }
  interface Events {
    'openit-hermes/ready'(): void
  }
}

export class HermesService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'hermes')
  }
  get status(): string {
    return 'openit-hermes ready'
  }
}

export function apply(ctx: Context): void {
  ctx.provide('hermes', new HermesService(ctx) as any)
  ctx.emit('openit-hermes/ready' as any)
}
