# Ondo 데드맨 강제 종료 시험

2026-10-29 KST. 첫 승인 시험에서 실제 타임아웃 취소와 미체결은 확인했지만 검사기의 실시간 판정은 실패했다. 파서를 실제 응답에 맞춰 수정했고 정상 감시 재시험은 새 주문 1건에 대한 별도 승인 대기 중이다. 기본 명령은 조회만 한다.

```
python -m olbot.ondo_trial
```

## 승인할 범위

- Ondo 메인넷 LIT 1개, 기준 시세 명목가 최대 5 USDC의 매수 호가 1건. 현재 조회 시 약 3 USDC다.
- 가격은 최우선 매수보다 10% 낮게 내림하고 post-only GTC로 보낸다. 시장 급변 시 체결될 수 있다.
- 계정 전체의 `cancelAllOrdersAfterPerps`를 30초로 설정한다. 이 기능은 해당 계정의 모든 호가를 취소할 수 있으므로, 기존 주문/포지션이 없어야 하며 시험 중 다른 봇·수동 주문을 같이 쓰면 안 된다.
- 전용 작업 프로세스에서 ACK와 주문이 열린 것을 확인한 뒤, 부모 프로세스가 그 작업 프로세스만 강제 종료한다. 실제 다른 앱이나 사용자 터미널은 종료하지 않는다.
- 자동 취소가 확인되지 않으면 해당 시험 주문 ID만 최대 3회 취소한다. 예외적으로 체결되면 확인된 체결 수량만 reduce-only IOC 매도 1건으로 정리한다. 시장가보다 1% 낮은 정리 가격 제한 때문에 전량 정리가 안 될 수도 있다. 수수료·손실 가능성과 미정리 노출 위험이 있다.
- 출금·이체·레버리지·키 등록·Lighter 주문·전체 봇 실운영은 포함하지 않는다.

승인받은 뒤에만 실행한다.

```
python -m olbot.ondo_trial --execute --approve-max-entry-usd 5 --approve-deadman --approve-cleanup
```

현재의 종료된 실패 기록으로 재시험할 때에는 별도 승인 후 아래 명령을 쓴다.

```
python -m olbot.ondo_trial --execute --approve-max-entry-usd 5 --approve-deadman --approve-cleanup --retry-resolved
```

이 옵션은 이전 주문의 무체결 타임아웃 취소와 현재 위험 해소가 확인된 실패 기록에만 적용된다. 이전 JSON은 `data/trial-history/`에 보존하고 새 번호를 전송 전에 저장한다. 미확정·체결·이미 통과한 기록에는 사용할 수 없다. 실행 lock이 중복 실행을 막으며, 중단된 lock 파일도 확인 없이 삭제하면 안 된다.

## 판정과 기록

- 데드맨 ACK 전에는 주문하지 않는다. 새 클라이언트는 기존 `OndoPrivateWS`와 `ReconnectingWS`를 상속하며 ACK 분류는 순수 함수다. 실제 수신 ACK와 최종 주문 응답을 회귀 자료로 수집했다. 마지막 하트비트 후 5~8초에 강제 종료하여 30초 관측 창에 여유를 둔다.
- 주문 번호와 상태는 전송 전에 `data/ondo-deadman-trial.json`에 저장하고 fsync한다. 기존 기록이 있으면 새 주문을 거부한다. 기본 명령으로 재실행하면 같은 번호를 조회만 한다.
- `canceled`와 `cancelReason=cancelAfterTimeout`, 체결 0, 실제 강제 종료, 30초 내 관측을 모두 만족해야 데드맨 통과다. 늦은 취소나 비상 수동 취소 성공을 데드맨 통과로 기록하지 않는다.
- 전송 결과 유실·조회 실패·누락/잘못된 수량·알 수 없는 상태를 미체결로 간주하지 않는다. 자동 재주문은 없다.
- 정리가 일부만 됐거나 결과가 불명확하면 기록을 남기고 멈춘다. 미확정 또는 데드맨 실패 기록이 있으면 전체 봇 live 시작도 차단한다. 파일을 지워 우회하지 않는다.
- 소액 시험 자금은 이 제한 시험용이다. 기존 500달러 운영 설정을 그대로 켜지 않는다.

공식 근거:

- [Create Order](https://docs.ondoperps.xyz/api-reference/orders/create-order)
- [Get Order by ID](https://docs.ondoperps.xyz/api-reference/orders/get-order-by-id)
- [Cancel Order by ID](https://docs.ondoperps.xyz/api-reference/orders/cancel-order-by-id)
- [Dead Man's Switch](https://docs.ondoperps.xyz/api-reference/private-channels/subscribe:-perps-dead-mans-switch)
