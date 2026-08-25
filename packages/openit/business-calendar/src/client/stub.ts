export type CalLevel = 0 | 1 | 2 | 3 | 4 | 5

export interface CalItem {
  id: string
  title: string
  start: string
  end: string
  attendees: string[]
  level: CalLevel
  loc?: string
}

export const CAL_STUB: CalItem[] = [
  { id: 'c1', title: '경영진 주간 회의', start: '오늘 10:00', end: '11:30', attendees: ['김대표', '박재무', '이팀장'], level: 0, loc: '대회의실 A' },
  { id: 'c2', title: '고객사 제안서 리뷰', start: '오늘 14:00', end: '15:00', attendees: ['이팀장', '최사원'], level: 2, loc: '회의실 B' },
  { id: 'c3', title: '팀 스프린트 플래닝', start: '내일 09:30', end: '10:30', attendees: ['이팀장', '정대리', '최사원'], level: 3, loc: 'Zoom' },
  { id: 'c4', title: '파트너사 미팅 — A사일정', start: '내일 16:00', end: '17:00', attendees: ['박재무', '김대표'], level: 1, loc: '강남 오피스' },
  { id: 'c5', title: '개인 — 병원 예약', start: '모레 10:00', end: '11:00', attendees: ['나'], level: 5, loc: '서울병원' },
  { id: 'c6', title: '전사 워크숍 준비', start: '금 13:00', end: '17:00', attendees: ['전체'], level: 2, loc: '워크숍장' },
]
