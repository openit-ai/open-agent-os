import { useEffect, useMemo, useState } from 'react'
import css from './BoardWidget.module.css'
import { BOARD_STUB } from './stub.ts'
import { filterByLevel, resolveUserLevel } from './permission.ts'

const COLS = [
  { key: 'todo' as const, label: '할 일' },
  { key: 'doing' as const, label: '진행 중' },
  { key: 'done' as const, label: '완료' },
]

export function BoardWidget() {
  const [level, setLevel] = useState<number>(2)
  useEffect(() => {
    try {
      const w = window as unknown as Record<string, unknown>
      const maybeCtx = w['__dsh_ctx'] ?? w['__cordis_ctx']
      if (maybeCtx) setLevel(resolveUserLevel(maybeCtx))
    } catch { /* fallback */ }
  }, [])

  const filtered = useMemo(() => filterByLevel(BOARD_STUB, level), [level])

  return (
    <div className={css.wrapper}>
      <div className={css.card}>
        <div className={css.header}>
          <div className={css.title}>업무보드</div>
          <span className={css.badge}>{filtered.length} 건 · L{level}</span>
        </div>
        <div className={css.board}>
          {COLS.map((col) => {
            const items = filtered.filter((b) => b.status === col.key)
            return (
              <div key={col.key} className={css.column}>
                <div className={css.colHead}>
                  <span>{col.label}</span>
                  <span className={css.count}>{items.length}</span>
                </div>
                {items.map((t) => (
                  <div key={t.id} className={css.task}>
                    <div className={css.taskTitle}>{t.title}</div>
                    <div className={css.taskMeta}>
                      <span className={css.assignee}>{t.assignee}</span>
                      <span>{t.due}</span>
                    </div>
                  </div>
                ))}
                {items.length === 0 && <div className={css.taskMeta} style={{ padding: 6 }}>없음</div>}
              </div>
            )
          })}
        </div>
        <div className={css.footer}>
          <span>권한 L{level} · {filtered.length}/{BOARD_STUB.length} 표시</span>
          <span>Notion 스텁</span>
        </div>
      </div>
    </div>
  )
}
