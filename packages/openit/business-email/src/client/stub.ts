export type EmailLevel = 0 | 1 | 2 | 3 | 4 | 5

export interface EmailItem {
  id: string
  from: string
  subject: string
  snippet: string
  time: string
  unread: boolean
  level: EmailLevel
}

export const EMAIL_STUB: EmailItem[] = [
  { id: 'm1', from: '김대표 <ceo@openit.kr>', subject: '[L0] 연간 매출 전략 회의 결과 공유', snippet: '올해 목표 대비 124% 달성, 내년 로드맵 초안 첨부합니다.', time: '09:42', unread: true, level: 0 },
  { id: 'm2', from: '박재무 <finance@openit.kr>', subject: '4분기 정산 리포트 (L1)', snippet: '고정비 8% 절감, 변동비 추이 그래프 포함.', time: '09:15', unread: true, level: 1 },
  { id: 'm3', from: '이팀장 <team@openit.kr>', subject: '신규 고객사 온보딩 일정', snippet: 'A사 킥오프 미팅이 내일 14시로 확정되었습니다.', time: '어제', unread: false, level: 2 },
  { id: 'm4', from: 'Gmail 알림', subject: '주간 뉴스레터 — 산업 동향 브리핑', snippet: '이번 주 SaaS 지표 요약과 경쟁사 동향을 정리했습니다.', time: '어제', unread: false, level: 2 },
  { id: 'm5', from: '최사원 <choi@openit.kr>', subject: '휴가 신청서 승인 요청', snippet: '12/28~12/29 연차 신청드립니다. 업무 인수인계 완료.', time: '2일 전', unread: true, level: 3 },
  { id: 'm6', from: '정대리 <jung@openit.kr>', subject: '[팀] 스프린트 회고록 공유', snippet: '이번 스프린트 Done 12건, 개선 포인트 3가지 정리.', time: '2일 전', unread: false, level: 3 },
  { id: 'm7', from: '나 <me@openit.kr>', subject: '내 예약 메모 — 개인', snippet: '내일 병원 예약 10시, 개인 일정 메모.', time: '3일 전', unread: false, level: 5 },
  { id: 'm8', from: 'Notion', subject: '내 업무보드 알림 요약', snippet: '할 일 3건이 마감 임박, 보드에서 확인하세요.', time: '3일 전', unread: false, level: 4 },
]
