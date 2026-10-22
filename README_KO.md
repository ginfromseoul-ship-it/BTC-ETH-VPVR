# BTC·ETH VPVR 자동 계산 — GitHub Actions 배포 파일

## 현재 한계
- 이 코드는 바이낸스 공식 BTCUSDT·ETHUSDT USD-M 5분봉 자료로 근사 거래량 프로필을 산출합니다.
- 틱별 실제 체결 데이터 및 TradingView VPVR과 같지 않습니다.
- 공식 아카이브의 월별·일별 ZIP 파일을 받아야 실행됩니다. 작성 환경에서는 외부 인터넷 파일 수집이 차단되어 실제 Binance end-to-end 성공을 검증하지 못했습니다.
- 데이터가 조금이라도 크게 누락되면 **수치를 창작하지 않고 실패**합니다. `data/volume_profile_summary.json`은 실행 성공시에만 갱신됩니다.

## 최초 1회 설정
1. GitHub 무료 계정으로 로그인합니다.
2. 새 **Public repository**를 만듭니다. (시장 데이터 수치만 공개되며 개인정보·거래 계좌를 올리지 않습니다.)
3. 이 ZIP의 압축을 풀어 `volume_profile_daily.py`, `requirements.txt`, `.github/workflows/daily.yml`을 **같은 경로 구조**로 저장소에 올립니다. GitHub 웹 편집기에서 `.`(마침표)를 눌러 github.dev를 사용해 폴더 구조를 만드는 것이 편합니다.
4. 저장소의 **Actions** 탭에서 **Daily Binance BTC ETH VPVR** 작업을 골라 **Run workflow**를 눌러 시험합니다.
5. 작업이 성공하면 `data/volume_profile_summary.json`이 생성됩니다. JSON의 generated_utc, data_end_utc_exclusive, 8개 결과(BTC·ETH의 30d/26w/52w, 추세 시작점 설정시 anchored)를 확인합니다.
6. 공개 저장소 주소를 ChatGPT에 알려줍니다. 이 주소를 기존 BTC·ETH 감시 작업에서 매일 확인하도록 별도 연결 설정할 수 있습니다. 단 실제 리포트에서 공개 JSON 읽기 성공 여부는 한 번 더 검증해야 합니다.

## 참고
- 매일 **22:23 UTC (07:23 KST)** 실행 예약입니다. GitHub Actions 스케줄은 지연되거나 누락될 수 있습니다. ChatGPT의 오전 8시 보고 전에 최신 갱신됐는지 확인해야 합니다.
- 주요 급락·급등의 시작일을 고정하려면 workflow 파일의 `ANCHOR_UTC`를 `YYYY-MM-DD` UTC 형식으로 지정합니다. 자동으로 상승·하락 시작일을 발견하는 기능은 아직 없습니다.
- 52주보다 오래된 앵커는 이 스크립트에서 지원하지 않습니다.
- GitHub의 Public 저장소 스케줄 작업은 저장소 활동이 60일간 없으면 정지될 수 있습니다.
- GitHub 저장소의 Action 설정에서 workflow 토큰의 콘텐츠 쓰기가 허용되어야 합니다.
