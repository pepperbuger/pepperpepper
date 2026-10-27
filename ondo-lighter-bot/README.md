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

## 집에서 이어가는 법

1. 저장소를 받고 이 폴더로 이동해서 위 설치를 한다.
2. 마켓 상태를 본다: `python3 scripts/ondo_depth_probe.py --csv depth.csv`
3. Claude Code를 이 폴더에서 열고 이렇게 요청한다:
   "docs/SPEC.md의 8. 구현 순서 3단계부터 진행해줘. 각 단계 완료 기준을 통과하면 멈추고 알려줘."
4. 3단계(Ondo 인증)와 5단계(Lighter 헷지) 전에 API 키를 만든다. 키 권한과 보관 방법은 SPEC 5번 6항을 따른다.
5. 메인넷 주문은 8단계에서만, 소액으로 시작한다.

## 1주차 목표

수익이 아니라 측정이다. 마켓별로 "호가 1,000달러를 1시간 걸었을 때 받는 리워드"와 "체결 1건당 헷지 비용"을 숫자로 뽑는다. 수요일 USDC 지급 후 이 숫자로 2주차 마켓과 크기를 정한다.
