/**
 * Cordis plugin "openit-permission" — service ctx.permission
 */
import { Context, Service } from '@deepseek-ai/cordis'

export const name = 'openit-permission'

declare module '@deepseek-ai/cordis' {
  interface Context {
    permission: PermissionService
  }
  interface Events {
    'openit-permission/ready'(): void
  }
}

export class PermissionService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'permission')
  }
  get status(): string {
    return 'openit-permission ready'
  }
}

export function apply(ctx: Context): void {
  ctx.provide('permission', new PermissionService(ctx) as any)
  ctx.emit('openit-permission/ready' as any)
}
