# 현재 시험 잔고용 체결·헷지 검증

사용자가 별도로 승인한 뒤 2026-10-29에 아래 단발 시험을 완료했다. 실행 직전 양쪽 포지션·활성 주문 0을 다시 확인했고, LIT 4개로 총 4건만 전송했다. Ondo 진입 12.5128 USD, Lighter 진입 12.500000 USD로 모두 승인 한도 이내였다. 양쪽 진입과 reduce-only 정리가 모두 전량 체결됐고 최종 포지션·활성 주문은 0이다. 시험 기록은 resolved/hedge_verified=true이며 추가 전송은 없다. 전체 봇 설정은 dry_run으로 유지한다.

| 시험 전후 자산 감소 기준 순비용 | USD |
|---|---:|
| Ondo | 0.140223 |
| Lighter | 0.011002 |
| 합계 | 0.151225 |

순비용에는 왕복 가격 변화와 수수료 등이 포함된다. 종료 자산은 두 번 조회해 동일한 값을 확인했다. 원본 자산과 실행 직전 확인 기록은 로컬 `data/paired-hedge-audit.json`에 보존하며 커밋하지 않는다. 실제 주문 응답 4개는 식별값을 치환한 회귀 자료로 보존했다. 이미 완료된 기록이 있으므로 아래 실행 명령을 다시 사용해도 새 진입은 거부한다.

## 준비한 범위

- LIT 한 마켓만 사용한다. 양쪽 최소 수량을 동시에 만족하는 수량을 실시간 메타데이터로 계산한다. 현재 시세 약 3 USD와 Ondo 단위 1 LIT 기준으로는 4 LIT, 거래소별 약 12 USD다. 진입 계획이 어느 쪽에서든 15 USD를 넘으면 실행하지 않는다. 양쪽 가용 증거금이 각각 15 USDC 이상이어야 한다.
- Ondo IOC 지정가 매수 1건으로 실제 체결을 시도한다. 가격 상한은 최우선 매도보다 0.2% 위다. 정규 maker 호가의 post-only 조건에 대한 별도 승인 시험이며, 거래 수수료와 손실이 발생할 수 있다.
- Ondo의 최종 상태와 정확한 체결 수량·포지션을 확인한 뒤, 운영 코드의 Hedger를 재시도 1회 설정으로 호출하여 Lighter 반대 매도 IOC를 최대 1건 전송한다. 가격 제한은 중간가 대비 15bps다. 최소 주문보다 작은 Ondo 부분 체결은 새 헷지로 만들지 않고 해당 Ondo 수량만 정리한다.
- 양쪽 결과가 확정되면 확인된 포지션만 reduce-only IOC로 각각 최대 1건씩 정리한다. 합계 주문 전송은 최대 4건이다. 정리 가격 제한은 현재 호가 대비 0.2%이며 정리 성공을 보장하지 않는다. 진입 한도 15 USD는 최대 손실 보장이 아니다.
- 결과가 불명확하면 같은 번호로 조회만 하고 추가 주문을 보내지 않는다. 정리가 일부만 체결돼도 자동 재전송하지 않는다. 이 경우 사람이 실제 주문·포지션을 확인해야 한다. 같은 계정에서 수동 거래나 다른 봇을 동시에 사용하면 안 된다.
- 입출금·이체·레버리지 변경·키 등록·전체 봇 운영은 이 범위에 포함하지 않는다.

## 조회와 실행

기본 명령은 계획만 조회한다. 주문·취소·데드맨 설정을 하지 않고 시험 기록이나 DB도 만들지 않는다.

```
python -m olbot.paired_trial
```

위 범위에 대한 사람의 별도 승인을 받은 뒤에만 아래 플래그를 사용한다.

```
python -m olbot.paired_trial --execute --approve-max-position-usd 15 --approve-cleanup
```

이전 시험 기록이 있으면 새 진입은 거부한다. 재실행 기본 명령은 로컬 기록의 상태만 보여준다. 양쪽 진입 결과가 이미 확정된 기록의 정리만 이어갈 때는 동일 승인 범위 안에서 `--execute` 대신 `--finish-known`을 쓴다. 기존 정리 번호가 있으면 조회만 하고 다시 보내지 않는다. 미확정 진입이나 시험 DB의 미확정 헷지가 있으면 이 옵션도 정리를 보내지 않는다.

## 기록과 통과 기준

`data/paired-hedge-trial.json`과 별도 `data/paired-hedge-trial.sqlite`에 기록한다. 주문 번호는 전송 전에 fsync하며, 운영용 DB는 읽기 전용으로 미확정 유무만 확인한다. 시험 DB의 미확정 기록은 운영 코드의 주문 최종 상태 대사로만 해제한다. 이 파일들을 지우거나 교체해서 우회하면 안 된다.

통과하려면 실제 Ondo 체결 수량이 0보다 크고, Lighter의 최종 체결 수량과 정확히 같아야 한다. 그다음 양쪽 정리 주문이 전량 체결되고 포지션·활성 주문이 모두 없어야 한다. 전체 봇은 미확정·미검증 시험이나 남아 있는 시험 DB 미확정이 있으면 live 시작을 거부한다. 이 제한 시험만으로 전체 봇의 WS 체결 이벤트 경로나 장시간 운영까지 검증되는 것은 아니다.

공식 근거: [Ondo Create Order](https://docs.ondoperps.xyz/api-reference/orders/create-order), [Ondo Get Order by ID](https://docs.ondoperps.xyz/api-reference/orders/get-order-by-id), [Lighter Order API](https://github.com/elliottech/lighter-python/blob/main/docs/OrderApi.md), [Lighter signer client](https://github.com/elliottech/lighter-python/blob/main/lighter/signer_client.py).
