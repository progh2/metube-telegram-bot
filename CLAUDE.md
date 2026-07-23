# CLAUDE.md

이 저장소를 Claude Code로 작업할 때 참고할 프로젝트 컨텍스트.

## 프로젝트 개요

텔레그램 봇이 유튜브(yt-dlp 지원 사이트) 링크를 받아 자가호스팅 MeTube 인스턴스의
`/add` API로 전달하는 단일 파일 파이썬 프로젝트. NAS(시놀로지)의 도커 컨테이너로 배포한다.

## 구조

- `bot.py` — 봇 전체 로직 (단일 파일). python-telegram-bot v21 (async) + requests 사용.
  - `on_message`: 메시지에서 URL 추출 → 형식 선택 인라인 버튼 표시 (mp4 / mp3 / both)
  - `on_button`: 콜백 처리 → `metube_add()`로 MeTube API 호출. `both`는 mp4, mp3 두 번 요청
  - callback_data는 64바이트 제한이 있어 URL 대신 짧은 토큰을 쓰고, 메모리 dict(`pending`)에 URL 보관 (재시작 시 소멸 — 의도된 동작)
  - MeTube 응답 판정: HTTP 200이고 `status != "error"`면 성공으로 간주 (content-type을 신뢰하지 말 것 — 과거 버그 원인)
- `Dockerfile` — python:3.12-slim, 의존성 pip 설치 후 bot.py 복사
- `docker-compose.example.yml` — 공개용 템플릿. 실제 값이 든 `docker-compose.yml`은 gitignore 대상

## 규칙

- 사용자에게 보이는 문자열(봇 응답)은 한국어 유지
- 시크릿(BOT_TOKEN, 챗 ID, 내부 IP)은 절대 커밋하지 않는다. 예시 값은 `YOUR_...` 플레이스홀더 사용
- 의존성 추가는 신중히 — 단일 파일 + 최소 의존성 유지가 목표

## 배포 (시놀로지)

- 코드는 이미지 안으로 COPY되므로 수정 후 반드시 재빌드:
  `docker-compose up -d --build` (캐시 문제 시 `build --no-cache`)
- Container Manager GUI에서는 프로젝트 빌드 후 중지→시작까지 해야 반영됨

## 테스트

- 자동 테스트: `python -m unittest -v` (`test_bot.py`)
  - 표준 라이브러리 `unittest`만 사용 — pytest 등 테스트 의존성을 추가하지 않는다
  - `FakeMeTube`가 실제 MeTube의 URL 기반 중복 판정을 흉내 내므로, 서버 없이 검증 가능
  - GitHub Actions(`.github/workflows/test.yml`)에서 push/PR마다 실행됨
- 문법: `python -m py_compile bot.py`
- 실제 동작 확인은 NAS에서: 봇에게 링크 전송 → 버튼 3개 표시 → MeTube 웹 UI(8081)에 대기열 등록 확인

**기능을 추가하거나 버그를 고치면 `test_bot.py`에 테스트를 함께 추가한다.**
버그 수정 테스트에는 어떤 이슈의 회귀인지 docstring으로 남긴다.
테스트가 실제로 회귀를 잡는지 확인하려면 수정을 일시적으로 무력화해 실패하는지 본다.

## MeTube API 참고

- `POST {METUBE_URL}/add` body: `{"url", "quality": "best", "format": "mp4"|"mp3", "auto_start": true}`
- 정상 응답: `{"status": "ok"}`
- **대기열 중복은 URL만으로 판정된다** (`format`은 키가 아님). 같은 URL을 mp4/mp3로 연달아
  보내면 두 번째는 버려지면서 `{"status": "ok", "msg": "Already in queue: ..."}` 를 HTTP 200으로
  반환한다 — 성공으로 오판하기 쉬우니 주의. 완료(`done`)된 항목은 중복 검사 대상이 아니라
  다시 등록할 수 있다 (이슈 #1의 `둘 다` 구현 근거)
- 재생목록 URL은 MeTube가 알아서 전체 등록. `noplaylist` 등 yt-dlp 옵션은 MeTube 쪽 `YTDL_OPTIONS` 환경 변수로 제어 (봇이 아니라 MeTube 컨테이너 설정)
