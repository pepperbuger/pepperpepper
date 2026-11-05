# Ondo 실제 응답 출처

2026-10-29 KST, 사용자 승인으로 실행한 Ondo 메인넷 LIT 1개 post-only 시험의 최종 `GET /v1/perps/orders/client:{id}` 응답에서 필요한 필드만 추출했다. 원본 주문·클라이언트 식별값은 테스트용 `fixture_order`, `probe_a`로 치환하고, 원본 시각·계정·키·서명은 제외했다.

실제 응답은 false인 `reduceOnly`를 생략했고, 취소 사유는 문서 enum의 `cancelAfterTimeout`과 다른 소문자 `cancelaftertimeout`이었다. 수량·체결 등 필수 필드의 누락을 0으로 바꾸지는 않는다. 정리 주문은 여전히 `reduceOnly: true` 확인을 요구한다.

`ondo-deadman-acks.json`은 같은 실행에서 실제 수신한 로그인/데드맨 구독 ACK의 타입·채널만 추출한 것이다. 응답은 실제 자동 취소와 미체결을 입증하지만, 최초 검사 프로그램의 실시간 시간 판정까지 정상 통과했다는 뜻은 아니다. 첫 실행의 엄격한 데드맨 판정은 실패로 보존했다.
