import { useEffect, useMemo, useRef, useState } from 'react'
import css from './SalesWidget.module.css'
import { HISTORY_KEY, MAX_HISTORY, SALES_STUB } from './stub.ts'
import { resolveUserLevel } from './permission.ts'

function useCountUp(value: number, duration = 800): number {
  const [display, setDisplay] = useState(value)
  const prev = useRef(value)
  useEffect(() => {
    const from = prev.current
    const to = value
    if (from === to) return
    const start = performance.now()
    let raf = 0
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration)
      const eased = 1 - Math.pow(1 - t, 3)
      setDisplay(Math.round(from + (to - from) * eased))
      if (t < 1) raf = requestAnimationFrame(tick)
      else prev.current = to
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [value, duration])
  // keep prev in sync when value changes externally
  useEffect(() => { if (prev.current !== value) prev.current = value }, [value])
  return display
}

function formatKRW(n: number): string {
  return new Intl.NumberFormat('ko-KR').format(n) + '원'
}

function loadHistory(): number[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    if (!raw) return [...SALES_STUB.sparkline]
    const arr = JSON.parse(raw) as number[]
    if (Array.isArray(arr) && arr.every((x) => typeof x === 'number')) return arr.slice(-MAX_HISTORY)
  } catch { /* ignore */ }
  return [...SALES_STUB.sparkline]
}
function saveHistory(arr: number[]): void {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(arr.slice(-MAX_HISTORY))) } catch { /* ignore */ }
}

function drawSparkline(canvas: HTMLCanvasElement, data: number[]): void {
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  // server-monitor pattern: clientWidth + retina, fallback 220
  const pw = canvas.parentElement?.clientWidth || 220
  const w = pw * 2
  const h = 220
  canvas.width = w
  canvas.height = h
  canvas.style.height = '110px'
  ctx.clearRect(0, 0, w, h)

  if (data.length === 0) {
    ctx.fillStyle = 'rgba(255,255,255,.03)'
    ctx.fillRect(0, 0, w, h)
    return
  }
  if (data.length === 1) {
    ctx.strokeStyle = '#533afd'
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.moveTo(4, h / 2)
    ctx.lineTo(w - 4, h / 2)
    ctx.stroke()
    return
  }
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1
  const pad = 12
  const step = (w - pad * 2) / (data.length - 1)

  ctx.beginPath()
  data.forEach((v, i) => {
    const x = pad + i * step
    const y = h - pad - ((v - min) / range) * (h - pad * 2)
    if (i === 0) ctx.moveTo(x, y)
    else ctx.lineTo(x, y)
  })
  ctx.strokeStyle = '#533afd'
  ctx.lineWidth = 2
  ctx.lineJoin = 'round'
  ctx.lineCap = 'round'
  ctx.stroke()

  // gradient fill
  const lastY = h - pad - ((data[data.length - 1]! - min) / range) * (h - pad * 2)
  ctx.lineTo(pad + (data.length - 1) * step, h - pad)
  ctx.lineTo(pad, h - pad)
  ctx.closePath()
  const grad = ctx.createLinearGradient(0, 0, 0, h)
  grad.addColorStop(0, 'rgba(83,58,253,0.22)')
  grad.addColorStop(1, 'rgba(83,58,253,0.02)')
  ctx.fillStyle = grad
  ctx.fill()

  // last point highlight if needed
  void lastY
}

export function SalesWidget() {
  const [level] = useState<number>(() => {
    try {
      const w = window as unknown as Record<string, unknown>
      const maybeCtx = w['__dsh_ctx'] ?? w['__cordis_ctx']
      if (maybeCtx) return resolveUserLevel(maybeCtx)
    } catch { /* fallback */ }
    return 2
  })
  const [history, setHistory] = useState<number[]>(() => loadHistory())
  const todayDisp = useCountUp(SALES_STUB.kpi.today)
  const weekDisp = useCountUp(SALES_STUB.kpi.week)
  const monthDisp = useCountUp(SALES_STUB.kpi.month)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  // persist history
  useEffect(() => { saveHistory(history) }, [history])

  // draw sparkline with rAF after mount and on resize
  useEffect(() => {
    const c = canvasRef.current
    if (!c) return
    const paint = () => drawSparkline(c, history)
    // initial paint after layout
    const raf = requestAnimationFrame(paint)
    const onResize = () => requestAnimationFrame(paint)
    window.addEventListener('resize', onResize)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', onResize)
    }
  }, [history])

  // demo: append one tick every 6s to simulate live (keep 60)
  useEffect(() => {
    const id = window.setInterval(() => {
      setHistory((prev) => {
        const last = prev[prev.length - 1] ?? 80
        const next = Math.max(10, Math.round(last + (Math.random() * 20 - 8)))
        const arr = [...prev, next].slice(-MAX_HISTORY)
        return arr
      })
    }, 6000)
    return () => clearInterval(id)
  }, [])

  const avg = useMemo(() => history.length ? Math.round(history.reduce((a, b) => a + b, 0) / history.length) : 0, [history])
  const status: 'ok' | 'warning' | 'critical' = avg >= 70 ? 'ok' : avg >= 40 ? 'warning' : 'critical'
  const badgeClass = status === 'critical' ? css.badgeCrit : status === 'warning' ? css.badgeWarn : css.badgeOk
  const cardClass = status === 'critical' ? `${css.card} ${css.cardCritical}` : status === 'warning' ? `${css.card} ${css.cardWarning}` : css.card

  return (
    <div className={css.wrapper}>
      <div className={cardClass}>
        <div className={css.header}>
          <div className={css.title}>매출</div>
          <span className={`${css.badge} ${badgeClass}`}>{status === 'ok' ? '정상' : status === 'warning' ? '주의' : '위험'} · L{level}</span>
        </div>
        <div className={css.kpiGrid}>
          <div className={css.kpi}>
            <div className={css.kpiLabel}>오늘</div>
            <div className={css.kpiValue}>{formatKRW(todayDisp)}</div>
            <div className={css.kpiSub}>실시간 집계</div>
          </div>
          <div className={css.kpi}>
            <div className={css.kpiLabel}>이번 주</div>
            <div className={css.kpiValue}>{formatKRW(weekDisp)}</div>
            <div className={css.kpiSub}>월~오늘</div>
          </div>
          <div className={css.kpi}>
            <div className={css.kpiLabel}>이번 달</div>
            <div className={css.kpiValue}>{formatKRW(monthDisp)}</div>
            <div className={css.kpiSub}>누적</div>
          </div>
        </div>
        <div className={css.chartWrap}>
          <div className={css.chartTitle}>매출 스파크라인 (최근 {history.length}개 · localStorage 60개 유지)</div>
          <canvas ref={canvasRef} className={css.canvas} />
        </div>
        <div className={css.footer}>
          <span>평균 {avg} · 임계값 주의 ≥40 / 위험 &lt;40</span>
          <span>스텁 + 차트</span>
        </div>
      </div>
    </div>
  )
}
