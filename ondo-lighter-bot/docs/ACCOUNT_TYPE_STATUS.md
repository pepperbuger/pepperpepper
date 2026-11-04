# 현재 Lighter 계정 등급 — Standard 확인

2026-11-04 사용자가 직접 Standard로 전환했다고 알렸다. 같은 날20:34:47 KST 공식 `GET /api/v1/accountLimits` 읽기 전용 조회에서 다음을 확인했다.

- `user_tier: standard`
- `current_maker_fee_tick: 0`
- `current_taker_fee_tick: 0`
- Lighter 비영 포지션0·활성 주문0
- 에이전트의 등급 변경·주문·취소0, Ondo API 조회0

키는 기존 D설치본.env에서만 읽었고 로그/커밋에 남기지 않았다. 가린 증거는 task폴더 `outputs/standard-confirm-20261104/confirmation.json`에 있다. 기존config.yaml/.env/거래data와 과거시험 기록은 수정하지 않았다.

## 앞으로의 비교 기준

ETH를 기본 후보, META를TradFi1순위 후보로 비교한다. 같은500USD자본의 별도 대안이며 둘의 수익을 더하지 않는다. Standard 일반IOC헷지 수수료0, [공식 taker 지연300ms](https://docs.lighter.xyz/trading/trading-fees)를 적용한다. TWAP/RFQ 등 고급주문까지 무료라는 뜻은 아니다.

300ms는 공식 지연값이지 이번에 측정한 실제 체결 지연이 아니다. Premium140ms에서160ms 늘어난 구간의 가격 변화·스프레드·깊이·부분 체결을 비용에 반영해야 한다. 새 실주문이나 기존 관측기 재개는 실행하지 않았다.

## 과거 결과 보존

11월3일 BTC실주문시험과11월4일13:17경 공개 호가 비교는 당시Premium 기준이다. 그때의 실제 비용0.034696USDC나원본저널을Standard로소급수정하지않는다. 과거문서의"현재Premium"은당시이력이며이문서의새확인을우선한다.

같은 과거 호가에 수수료만0으로 바꾸는 대안은 지연 비용을 고정한 민감도 계산일 뿐 실제Standard수익이 아니다. 다음live직전에도현재등급·수수료·포지션·주문·미확정을다시확인한다. 이번사용자변경은새주문·상시운영승인이아니다.
