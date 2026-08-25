/**
 * Cordis plugin "openit-vault" — service ctx.vault
 */
import { Context, Service } from '@deepseek-ai/cordis'

export const name = 'openit-vault'

declare module '@deepseek-ai/cordis' {
  interface Context {
    vault: VaultService
  }
  interface Events {
    'openit-vault/ready'(): void
  }
}

export class VaultService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'vault')
  }
  get status(): string {
    return 'openit-vault ready'
  }
}

export function apply(ctx: Context): void {
  ctx.provide('vault', new VaultService(ctx) as any)
  ctx.emit('openit-vault/ready' as any)
}
