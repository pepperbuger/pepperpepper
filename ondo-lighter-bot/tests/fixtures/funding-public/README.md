# 공개 확정 펀딩 실제 응답

2026-10-30 KST 밤에 키 없이 조회한 Ondo BTC/ETH 이력과 Lighter BTC/ETH 이력이다. 비교 시작 전 신호와 초·밀리초·소수/percent·방향·누락 검증에 사용한다. Lighter의 같은 ETH 지급 시각 rate는 기존 실제 WS `paper-public/lighter-subscribed-market_stats-market_stats-0.json`과 대조했다. 계정 정보는 없다.

- https://docs.ondoperps.xyz/api-reference/funding-rate/get-funding-rate-history
- https://docs.lighter.xyz/api/funding/fundings
- https://docs.lighter.xyz/api/websocket

큰 count_back이 시작 시각 이전 자료까지 반환하는 별도 실제 응답은 로컬 실측 기록에 남겼으며, 목표 시간 필터 회귀 검사로 검증한다. 요청 시점이 서로 달라 이 fixture 자체의 최신 지급 시간이 다를 수 있다. 없는 시간은0이 아니다.
