import { useEffect, useMemo, useState } from 'react'
import css from './EmailWidget.module.css'
import { EMAIL_STUB } from './stub.ts'
import { filterByLevel, resolveUserLevel } from './permission.ts'

export function EmailWidget() {
  const [q, setQ] = useState('')
  const [onlyUnread, setOnlyUnread] = useState(false)
  const [level, setLevel] = useState<number>(2)

  // resolve permission once; ctx not available here, use window heuristic + fallback L2
  useEffect(() => {
    // Try to read permission from global cordis context if exposed (best-effort)
    try {
      const w = window as unknown as Record<string, unknown>
      const maybeCtx = w['__dsh_ctx'] ?? w['__cordis_ctx']
      if (maybeCtx) setLevel(resolveUserLevel(maybeCtx))
    } catch { /* fallback */ }
  }, [])

  const filtered = useMemo(() => {
    const byLevel = filterByLevel(EMAIL_STUB, level)
    return byLevel.filter((m) => {
      if (onlyUnread && !m.unread) return false
      if (!q) return true
      const qq = q.toLowerCase()
      return m.subject.toLowerCase().includes(qq) || m.from.toLowerCase().includes(qq) || m.snippet.toLowerCase().includes(qq)
    })
  }, [q, onlyUnread, level])

  const unreadCount = useMemo(() => filterByLevel(EMAIL_STUB, level).filter((m) => m.unread).length, [level])

  return (
    <div className={css.wrapper}>
      <div className={css.card}>
        <div className={css.header}>
          <div className={css.title}>이메일</div>
          <span className={css.badge}>{unreadCount} unread · L{level}</span>
        </div>
        <div className={css.controls}>
          <input
            className={css.search}
            placeholder="검색 — 제목, 발신자, 내용"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <button
            className={onlyUnread ? `${css.filterBtn} ${css.filterBtnActive}` : css.filterBtn}
            onClick={() => setOnlyUnread((v: boolean) => !v)}
            aria-pressed={onlyUnread}
          >
            안읽음만
          </button>
        </div>
        <div className={css.list}>
          {filtered.map((m: typeof EMAIL_STUB[number]) => (
            <div key={m.id} className={m.unread ? `${css.item} ${css.itemUnread}` : css.item}>
              <div className={css.itemHead}>
                <span className={css.from}>{m.from}</span>
                <span className={css.time}>{m.time}</span>
                {m.unread && <span className={css.dot} aria-label="unread" />}
              </div>
              <div className={css.subject}>{m.subject}</div>
              <div className={css.snippet}>{m.snippet}</div>
            </div>
          ))}
          {filtered.length === 0 && <div className={css.snippet} style={{ padding: 8 }}>결과가 없습니다.</div>}
        </div>
        <div className={css.footer}>
          <span className={css.levelHint}>권한 L{level} · {filtered.length}/{EMAIL_STUB.length} 표시</span>
          <span>Gmail 스텁</span>
        </div>
      </div>
    </div>
  )
}
