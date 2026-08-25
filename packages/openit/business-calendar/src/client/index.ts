import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
import type {} from '@deepseek-ai/dsh-client-ui-layout/client'
import { CalendarWidget } from './CalendarWidget.tsx'

export const inject = ['slots']

export function apply(ctx: ClientContext): void {
  ctx.effect(() => ctx.slots.register({ name: 'shell.overlay', id: 'openit-business-calendar' }, CalendarWidget), 'openit-business-calendar: shell.overlay')
}
