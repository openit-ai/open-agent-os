export type BoardLevel = 0 | 1 | 2 | 3 | 4 | 5

export interface BoardItem {
  id: string
  title: string
  status: 'todo' | 'doing' | 'done'
  assignee: string
  level: BoardLevel
  due: string
}

export const BOARD_STUB: BoardItem[] = [
  { id: 'b1', title: '홈페이지 리뉴얼 기획안 작성', status: 'doing', assignee: '이팀장', level: 2, due: '오늘' },
  { id: 'b2', title: '연간 매출 전략 보고서 초안', status: 'todo', assignee: '박재무', level: 0, due: '내일' },
  { id: 'b3', title: '고객사 A 온보딩 체크리스트', status: 'done', assignee: '최사원', level: 3, due: '어제' },
  { id: 'b4', title: 'API 문서 번역 및 정리', status: 'todo', assignee: '정대리', level: 3, due: '금' },
  { id: 'b5', title: '신규 채용 공고 작성', status: 'doing', assignee: '김대표', level: 1, due: '내일' },
  { id: 'b6', title: '내 개인 — 도서 구매 정리', status: 'todo', assignee: '나', level: 5, due: '모레' },
  { id: 'b7', title: '팀 회고 액션 아이템 3건', status: 'done', assignee: '이팀장', level: 3, due: '어제' },
  { id: 'b8', title: '보안 점검 체크리스트', status: 'doing', assignee: '정대리', level: 2, due: '오늘' },
  { id: 'b9', title: '파트너 미팅 후속 메일', status: 'todo', assignee: '최사원', level: 4, due: '오늘' },
]
