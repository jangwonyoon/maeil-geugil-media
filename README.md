# maeil-geugil-media

매일 그 길(@maeil.geugil) 예약 게시 저장소. 맥북이 꺼져 있어도 GitHub Actions 가 15분마다 큐를 보고 Instagram 에 올린다.

> **공개 저장소다.** `queue/` 에 올린 캡션·이미지는 게시 전에도 누구나 볼 수 있다. 엠바고·비공개 내용은 올리지 말 것.

## 폴더

| 경로 | 뜻 |
|---|---|
| `queue/<YYYY-MM-DD_HHMM>_<slug>/` | 게시 대기. `post.json` + `NN.jpg` + `caption.txt` |
| `posted/…` | 게시됨. `post.json` 에 `media_id`·`permalink`·`published_at` |
| `stale/…` | 예약 시각이 3시간 넘게 지나 게시하지 않음 |
| `failed/…` | 3회 실패 또는 같은 캡션 중복으로 멈춤. `last_error` 확인 |
| `scripts/` | `enqueue.py`(로컬 등록), `publish_queue.py`(Actions 게시), `refresh_token.py`(로컬 토큰 갱신) |

`post.json`: `publish_at`(KST ISO), `caption`(파일명), `images[{file,bytes,sha256}]`, `kind`(image/carousel), `status`, `attempts`. 위치 태그 없음. 1장이면 단일 이미지, 2장 이상이면 캐러셀.

## 1. 등록 (로컬)

```bash
cd ~/maeil-geugil-work/maeil-geugil-media
uv run scripts/enqueue.py ~/maeil-geugil-work/2026-11-20_flash-template/out/<slug> \
  ~/maeil-geugil-work/2026-11-20_flash-template/out/<slug>/캡션.txt --at "2026-11-21 08:00"
```

- PNG → JPEG(품질 92, sRGB, 1080×1350) 변환, `queue/` 폴더 생성, 커밋·push 까지 한다.
- 폴더 이름 slug 는 덱 이름에서 영문·숫자만 뽑는다. 원하면 `--slug gtx-c`.
- 거절: 같은 시각(분)에 이미 항목이 있을 때, 시각이 지났을 때, 1080×1350 이 아닐 때, 캡션 2200자·해시태그 30개 초과.
- 예약 시각은 최소 15~20분 뒤로. raw 주소 반영과 cron 간격 때문이다.
- 취소: `git rm -r queue/<폴더> && git commit -m "unqueue: …" && git push`.
- 시각 변경: 취소 후 다시 등록.

## 2. 자동 게시 (Actions `publish`)

15분마다(`*/15 * * * *`) 실행. GitHub 예약 실행은 수십 분 늦을 수 있다.

1. 토큰으로 `/me` 호출. 실패하면 워크플로 실패 (토큰 만료·폐기 감지).
2. `publish_at` 이 지난 `queued` 항목을 오래된 순으로 최대 2개.
3. 예약 시각이 3시간 넘게 지났으면 게시하지 않고 `stale/` 로 옮기고 워크플로 실패로 알린다 (아침 속보가 저녁에 올라가지 않게).
4. 최근 게시물에 같은 캡션이 있으면: 등록 이후 것이면 이전 실행 결과로 보고 기록만 복구, 등록 이전 것이면 중복으로 `failed/`.
5. 이미지 주소(`raw.githubusercontent.com/<repo>/<커밋 sha>/queue/…`)를 HEAD 로 200·`image/jpeg` 확인, 내용 해시 대조. 아직 404 면 다음 실행으로 미룸(시도 횟수에 안 넣음).
6. `content_publishing_limit` 확인. 한도가 차면 미룸.
7. 컨테이너 생성(캐러셀은 children → CAROUSEL) → FINISHED 대기 → `media_publish`.
8. 성공: `post.json` 기록 후 `posted/` 로 옮겨 커밋. 실패: `attempts`·`last_error` 기록, 3회째면 `failed/` 로 옮기고 워크플로 실패.

`concurrency: publish-queue` 로 동시 실행을 막는다.

수동 실행: Actions → publish → Run workflow. `dry_run` 기본값은 true(큐 읽기·이미지 확인·컨테이너 생성까지, 게시·커밋 없음). 실제 게시를 손으로 당기려면 false.

`post.json` 에 `"test": true` 인 항목은 dry_run 에서만 다루고 실제 실행에서는 늘 건너뛴다.

## 3. 확인

- 게시 결과: `git pull` 후 `posted/<폴더>/post.json` 의 `permalink`, 또는 Actions 실행 요약 표.
- 실패 알림: 워크플로 실패 시 GitHub 가 메일을 보낸다.

## 실패 시 대처

| 증상 | 대처 |
|---|---|
| `토큰 확인 실패` | 토큰 만료·폐기. 로컬에서 `python3 scripts/refresh_token.py` 실행 (만료 전이면 갱신됨). 이미 만료됐으면 Meta 앱에서 새 장기 토큰을 받아 `~/reel-studio/.env` 의 `INSTAGRAM_ACCESS_TOKEN_MAEIL_GEUGIL` 교체 후 `gh secret set IG_TOKEN_MAEIL_GEUGIL -R jangwonyoon/maeil-geugil-media` |
| `stale/` 로 감 | 예약 실행이 3시간 넘게 밀림. 아직 올릴 가치가 있으면 폴더를 `queue/` 로 옮기고 `publish_at`·`status: queued` 를 고쳐 push |
| `failed/` 로 감 | `post.json` 의 `last_error` 확인 → 원인 고친 뒤 `queue/` 로 옮기고 `status: queued`, `attempts: 0` 으로 push. 같은 캡션 중복이면 이미 올라간 것인지 계정에서 먼저 확인 |
| 계속 `미룸: 이미지 주소 HTTP 404` | raw CDN 이 404 를 캐시함. 보통 몇 분 뒤 풀린다. 3시간 넘으면 stale 처리됨 |
| 한도 소진 | 24시간 게시 한도. 다음 실행에서 자동 재시도 |
| 예약 실행이 안 돈다 | 공개 저장소에서 60일간 활동이 없으면 GitHub 가 schedule 을 끈다. Actions 탭에서 다시 켠다 |

## 토큰 갱신 (로컬 launchd)

`~/Library/LaunchAgents/com.maeil-geugil.token-refresh.plist` 가 매주 월요일 09:10 에 `scripts/refresh_token.py` 를 실행한다 (맥북이 꺼져 있었으면 깨어난 뒤 실행).

- `graph.instagram.com/refresh_access_token` 으로 60일 연장 → `.env` 백업(`~/.local/state/maeil-geugil/env-backups/`, 최근 5개) 후 교체(권한 600) → `gh secret set` 으로 저장소 시크릿 갱신.
- 로그: `~/Library/Logs/maeil-geugil-token.log` (만료일·성공 여부만, 토큰 값 없음).
- 끄기: `launchctl bootout gui/$(id -u)/com.maeil-geugil.token-refresh`

맥북을 60일 넘게 켜지 않으면 갱신이 안 돼 토큰이 만료된다. 그때는 Actions 가 `토큰 확인 실패` 로 알린다.
