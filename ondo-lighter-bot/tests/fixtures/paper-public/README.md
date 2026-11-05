# 공개 WS 실제 수신 fixture

2026-10-30 20:31 KST 전후, 키·로그인 없이 70초 공개 구독에서 수신했다. Ondo top/trades/funding 및 Lighter ETH order_book 최초 전체 스냅샷·첫 델타·market_stats를 그대로 보존했다. 개인 계정 응답이 아니며 키/서명/계정 주문 데이터가 없다.

- [Ondo 색인](https://docs.ondoperps.xyz/llms.txt): tradesPerps, fundingRatesPerps, topOfBooksPerps.
- [Lighter 공식 WS](https://docs.lighter.xyz/api/websocket): order_book 스냅샷/델타와 begin_nonce 연결, market_stats percent 단위 funding.
- `apidocs.lighter.xyz/llms.txt`가 HTML 문서로 이동하여 거기에 연결된 `docs.lighter.xyz/llms.txt`와 `api/websocket`에서 현재 스키마를 확인했다.

가격/수량/펀딩 파서는 순수 함수로 검사한다. 초기 nonce와 첫 델타 begin_nonce가 일치하는지도 실제 자료로 검증한다.
