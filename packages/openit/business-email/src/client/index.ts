import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
import type {} from '@deepseek-ai/dsh-client-ui-layout/client'
import { EmailWidget } from './EmailWidget.tsx'

export const inject = ['slots']

export function apply(ctx: ClientContext): void {
  ctx.effect(() => ctx.slots.register({ name: 'shell.overlay', id: 'openit-business-email' }, EmailWidget), 'openit-business-email: shell.overlay')
}
