import { useEffect, useMemo, useState } from 'react'
import css from './CalendarWidget.module.css'
import { CAL_STUB } from './stub.ts'
import { filterByLevel, resolveUserLevel } from './permission.ts'

export function CalendarWidget() {
  const [level, setLevel] = useState<number>(2)
  useEffect(() => {
    try {
      const w = window as unknown as Record<string, unknown>
      const maybeCtx = w['__dsh_ctx'] ?? w['__cordis_ctx']
      if (maybeCtx) setLevel(resolveUserLevel(maybeCtx))
    } catch { /* fallback */ }
  }, [])

  const filtered = useMemo(() => filterByLevel(CAL_STUB, level), [level])

  return (
    <div className={css.wrapper}>
      <div className={css.card}>
        <div className={css.header}>
          <div className={css.title}>캘린더</div>
          <span className={css.badge}>{filtered.length} 일정 · L{level}</span>
        </div>
        <div className={css.grid}>
          {filtered.map((c) => (
            <div key={c.id} className={css.item}>
              <div className={css.timeCol}>
                <div className={css.start}>{c.start}</div>
                <div className={css.end}>{c.end}</div>
              </div>
              <div className={css.body}>
                <div className={css.evtTitle}>{c.title}</div>
                <div className={css.meta}>{c.loc} · {c.attendees.join(', ')}</div>
              </div>
            </div>
          ))}
          {filtered.length === 0 && <div className={css.meta} style={{ padding: 8 }}>표시할 일정이 없습니다.</div>}
        </div>
        <div className={css.footer}>
          <span>권한 L{level} · {filtered.length}/{CAL_STUB.length} 표시</span>
          <span>Google Calendar 스텁</span>
        </div>
      </div>
    </div>
  )
}
