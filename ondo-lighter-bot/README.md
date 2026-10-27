# Ondo + Lighter 유동성 리워드 봇

Ondo Perps에 지정가(post-only) 호가를 걸어 USDC 리워드와 포인트를 받고, 체결되면 Lighter(수수료 0)에서 반대 포지션을 잡아 가격 위험을 지우는 봇의 설계와 준비물이다. 코드는 집에서 Claude Code로 단계별로 만든다.

## 들어 있는 것

| 파일 | 내용 |
|---|---|
| [OVERVIEW.md](OVERVIEW.md) | 개요: 무엇을, 왜 만드는지, 목표와 판단 기준 |
| [docs/SPEC.md](docs/SPEC.md) | 설계서: 확인된 API 사실, 구조, 호가와 헷지 로직, 위험 관리, 측정, 구현 순서 |
| [CLAUDE.md](CLAUDE.md) | Claude Code가 이 폴더에서 지킬 규칙 (자동으로 읽힘) |
| [config.example.yaml](config.example.yaml) | 설정 예시 |
| [.env.example](.env.example) | 비밀값 예시 |
| [scripts/ondo_depth_probe.py](scripts/ondo_depth_probe.py) | 마켓 선별용 측정기. 지금 바로 실행 가능, API 키 불필요 |

## 진행 상황

| 단계 | 상태 |
|---|---|
| 1. 골격과 설정 | 완료 |
| 2. 시세 수신과 괴리 기록 | 완료 |
| 3. Ondo 인증 ~ 8. 소액 실운영 | 집에서 진행 |

## 설치와 실행 (1~2단계)

Python 3.11 이상이 필요하다. 윈도우는 `.venv/bin/` 대신 `.venv\Scripts\`를 쓴다.

```
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config.example.yaml config.yaml
cp .env.example .env

.venv/bin/python -m pytest                                  # 테스트
.venv/bin/python -m olbot check-config --online             # 1단계: 설정 검증 + 마켓 존재 확인
.venv/bin/python -m olbot watch --markets LIT,NEAR,ENA,SOL,XRP --duration 3600   # 2단계
```

`watch`는 주문을 보내지 않는다. 두 거래소 공개 시세만 받아서 `data/olbot.sqlite`에 괴리를 기록하고, 끝나면 마켓별 요약을 출력한다.

## 2단계 검증 결과 (2026-10-27 10:43~11:43 UTC)

`watch --markets LIT,NEAR,ENA,SOL,XRP --duration 3600` 결과. 완료 기준(1시간 끊김 없이 5개 마켓 기록)을 통과했다.

- 연결: 두 거래소 모두 끊김 0회, 재연결 0회
- 기록: 마켓당 360개 샘플 중 359개가 양쪽 호가 있음 (첫 샘플은 연결 전), 최대 공백 10초

| 마켓 | 괴리 중앙값(bps) | 최소 | 최대 | 표준편차 | dev_p95 |
|---|---|---|---|---|---|
| SOL | 16.7 | 13.2 | 19.3 | 0.8 | 1.7 |
| XRP | 18.0 | 4.9 | 34.8 | 1.6 | 2.1 |
| ENA | 4.4 | -11.1 | 19.2 | 2.0 | 3.2 |
| NEAR | 16.3 | -6.5 | 47.8 | 5.1 | 11.3 |
| LIT | 31.7 | 12.4 | 73.1 | 5.7 | 14.2 |

괴리는 Lighter 중간가가 Ondo보다 비싼 정도. dev_p95는 괴리가 중앙값에서 벗어난 폭의 95% 수준이다.

읽는 법:
- SOL, XRP, ENA는 괴리가 안정적이라 헷지가 깔끔하다.
- LIT, NEAR는 Ondo 호가가 얇은 만큼 괴리도 흔들린다(LIT는 한때 중앙값보다 41bps 벗어남). 유동성 리워드 몫이 가장 큰 마켓이 헷지 비용도 가장 큰 마켓이라는 뜻이다. 기본값 `basis_deviation_bps: 15`이면 LIT는 시간의 약 5%를 쉬게 된다.
- NEAR는 처음 몇 분 괴리가 약 2bps였다가 1시간 중앙값은 16bps였다. 중앙값 기준점 자체도 움직이므로 1시간 창이 적당한지 하루 데이터로 다시 본다.

## 집에서 이어가는 법

자세한 인계 내용과 Claude Code에 붙여넣을 요청문은 [docs/HANDOFF.md](docs/HANDOFF.md)에 있다.

1. 저장소를 받고 이 폴더로 이동해서 위 설치를 한다.
2. 마켓 상태를 본다: `python3 scripts/ondo_depth_probe.py --csv depth.csv`
3. Claude Code를 이 폴더에서 열고 이렇게 요청한다:
   "docs/SPEC.md의 8. 구현 순서 3단계부터 진행해줘. 각 단계 완료 기준을 통과하면 멈추고 알려줘."
4. 3단계(Ondo 인증)와 5단계(Lighter 헷지) 전에 API 키를 만든다. 키 권한과 보관 방법은 SPEC 5번 6항을 따른다.
5. 메인넷 주문은 8단계에서만, 소액으로 시작한다.

## 1주차 목표

수익이 아니라 측정이다. 마켓별로 "호가 1,000달러를 1시간 걸었을 때 받는 리워드"와 "체결 1건당 헷지 비용"을 숫자로 뽑는다. 수요일 USDC 지급 후 이 숫자로 2주차 마켓과 크기를 정한다.
