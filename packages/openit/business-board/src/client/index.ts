import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
import type {} from '@deepseek-ai/dsh-client-ui-layout/client'
import { BoardWidget } from './BoardWidget.tsx'

export const inject = ['slots']

export function apply(ctx: ClientContext): void {
  ctx.effect(() => ctx.slots.register({ name: 'shell.overlay', id: 'openit-business-board' }, BoardWidget), 'openit-business-board: shell.overlay')
}
