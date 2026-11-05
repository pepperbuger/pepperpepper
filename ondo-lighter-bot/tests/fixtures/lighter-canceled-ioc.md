# 실측 데이터 출처

2026-10-29 KST, 사용자가 승인한 Lighter 메인넷 LIT 단발 IOC 매수 주문의 `GET /api/v1/accountOrders` 실제 응답에서 추출했다.

`client_order_index`는 41, `owner_account_index`는 7로 치환했다. 테스트용 식별값이며 실제 주문 번호와 계정 번호가 아니다. 나머지 수록 필드의 값과 자료형은 실제 응답 그대로다. 주소, 원본 시각, 서명, 토큰, API 키는 저장하지 않았다.

주문은 `canceled-too-much-slippage`, 수량 3.54, 체결 0.00으로 종료됐다. `remaining_base_amount`는 0.00이었다. 포지션과 활성 주문도 모두 0을 확인했다. 무체결 취소 경로의 실측이며 실제 체결, 부분 체결, 헷지, Ondo 데드맨 실행은 아직 검증하지 않았다.
