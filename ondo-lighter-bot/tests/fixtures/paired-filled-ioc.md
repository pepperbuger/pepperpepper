# 실제 단발 체결·헷지 시험 응답

2026-10-29 KST, 사람이 승인한 LIT 최소 공통 수량 4개·거래소별 진입 최대 15 USD·정리 포함 최대 4건 시험에서 수집했다. 시작 직전 두 거래소 포지션과 활성 주문은 모두 0이었다.

- `ondo-filled-ioc-entry.json`: 매수 4개 전량 체결. 실제 응답에서 false인 reduceOnly는 생략됐다.
- `ondo-filled-ioc-close.json`: reduceOnly=true 매도 4개 전량 체결.
- `lighter-filled-ioc-entry.json`: 반대 매도 4.00개 전량 체결. 운영 Hedger를 재시도 1회로 호출했다.
- `lighter-filled-ioc-close.json`: reduce_only=true 매수 4.00개 전량 체결.

필요한 필드만 남겼다. Ondo clientOrderId는 fixture_entry/fixture_close, Lighter client_order_index는 43/44, owner_account_index는 7로 치환했다. 가격·수량·상태·속성·체결 금액은 실제 응답 값이다. 키, 계정 식별값, 주문 해시, 시각은 포함하지 않는다.

총 전송 4건, 재전송 0건. 최종 포지션과 활성 주문은 모두 0이며 시험 기록은 resolved/hedge_verified=true다. 자산 전후 감소액은 Ondo 0.140223 USD, Lighter 0.011002 USD, 합계 0.151225 USD였다. 이는 왕복 가격 변화와 수수료 등을 합친 순비용이다. 두 번의 종료 자산 조회가 일치했다. 실제 잔고 원본은 설치 폴더의 별도 로컬 감사 기록에만 보존한다.
