# REDESIGN W01 — 구독 자동화와 비전문가 UX로의 전면 재설계

대상: `film-unit-mv-studio` main (작성일 2026-10-19, §7 소유자 결정 반영 개정
2026-10-20). 작성 근거: JunTae 요구사항
(2026-10-19) — "앱이 실제로 영상을 만들지 못하고 API 키에 의존하는 것은 원래 목적
(이미 내고 있는 AI 구독을 활용해 고품질 MV를 만드는 '쌀먹')의 배신이다."

이 문서는 REDESIGN W00(`docs/REDESIGN_W00.md`)을 **대체하지 않고 그 위에서 방향을
바꾼다**. 구체적으로 W00 §6 결정 2("소비자 영상 앱의 무-스크립팅 규칙 유지",
2026-10-15 소유자 대리인 결정)를 **소유자의 새 지시(2026-10-19)가 명시적으로 뒤집는다**:
이제 브라우저·데스크톱 자동화가 목표 경로다. 소유자가 2026-10-20에 이 번복을
포함한 관련 결정 전부를 확정했다(§7 "결정 완료").

목표 우선순위 (소유자 지시 순서):

1. **비전문가 UX**: AI나 영상을 모르는 사람이 LLM과 샷 단위로 상세 콘티를 함께
   쓰기만 하면 한 번에 좋은 결과가 나오는 web UX/UI.
2. **API 키 제거**: 생성은 사용자가 이미 내고 있는 구독 서비스를 브라우저·데스크톱
   자동화(예: Windows PowerShell + 브라우저 자동화)로 돌린다. 유료 API 없음.
3. **전면 재설계**: 위 두 개를 위해 제품 표면을 다시 설계한다.

---

## 1. 솔직한 갭 분석 — 지금 왜 영상이 안 만들어지는가

### 1-1. 실제 AI 영상 생성 기록이 0건이다

저장소가 스스로 인정한다:

- `README.md:7` — "실제 외부 AI 영상 생성은 이 저장소의 검증 기록상 **0건**이며,
  자동 시각 의미 QC와 완성된 실곡 MV는 아직 검증하지 않았습니다."
- `docs/V03_ACCEPTANCE_REPORT.md:39` — "No paid video generation was submitted…
  it does not establish animation quality or completion of the user's real music
  video."
- `engine/segment_gen.py:45-49` (모듈 docstring) — 연결된 생성 adapter는
  `fake_segment`(FAKE/UNQUALIFIED)와 `rife_onnx`(로컬 보간, UNQUALIFIED)뿐이고
  `gemini_video`는 submit 경로가 거부하는 선언만 있다.
- `docs/evidence/anim-015-rife/demo.json` — RIFE 데모가 `ran:false`,
  `output_sha256:null`을 기록.
- `docs/CHEAP_ANIMATION_TEST.md:3` — 2026-09-11에 준비한 5초 모션 비교를 실행한
  적이 없다.

즉 컴파일러(`engine/compiler.py`, `engine/builds.py`)는 진짜 MP4를 만들지만,
그 MP4 안에 들어갈 **생성된 샷 영상이 한 번도 존재한 적이 없다**. Preview의
영상은 슬레이트·placeholder·사람이 직접 가져온 파일뿐이다.

### 1-2. API 키 없이는 자동으로 도는 단계가 없다

- `engine/providers.py` — 원격 "자동" provider는 전부 키를 요구한다:
  `ANTHROPIC_API_KEY`(:60), `GEMINI_API_KEY`(:65), `GROQ_API_KEY`(:69),
  `OPENROUTER_API_KEY`(:72), `CLOUDFLARE_API_TOKEN`(:75), `OPENAI_API_KEY`(:80),
  `XAI_API_KEY`(:83). 예외는 로컬·직접입력 provider다: `ollama`(:85),
  `lmstudio`(:88), `custom_openai`(:91)는 비밀키 대신 `base_url`을 받는다
  (custom_openai의 `CUSTOM_LLM_API_KEY`는 선택 사항, :92). video 단계의
  유일한 자동 선택지는 `gemini_video`(Veo 3.1 Lite, paid, `GEMINI_API_KEY`
  :113)와 `fal_video`(Wan 2.2 Turbo, paid, `FAL_KEY` :117) — 둘 다
  키+종량제다(:112-118). `mock_video`(:121)는 테스트 전용이다.
- `engine/settings.py:54-83` — 키는 `~/.film_unit/secrets.json`(또는
  `FILM_UNIT_HOME`)에 저장. 앱 UI(`app/models_ui.py` `_provider_form`)가 키
  입력 폼을 그린다.
- `engine/gemini.py:1-7` — Nano Banana 첫 프레임과 Veo 영상의 유료 호출 경로.
  `README.md:193-208`은 심지어 Google Cloud 결제 계정 생성 → Developer Program
  크레딧 → AI Studio 키 발급 → 지출 한도 설정이라는 **5단계 유료 API 설정 절차**를
  "처음 한 번 설정"으로 안내한다. 이것이 현재 앱의 유일한 end-to-end 자동 생성
  경로이며, 소유자가 거부하는 바로 그 모델이다.
- `engine/autopilot.py` — `autopilot()`은 `NEEDS_IMAGE_APPROVAL`(:51),
  `NEEDS_VIDEO_APPROVAL`(:144)에서 견적 승인을 요구하며 멈춘다. 즉 "자동"
  경로는 유료 API 경로와 동의어다.

### 1-3. 구독 경로는 자동화가 의도적으로 막혀 있다

- `engine/handoff.py:3-6` (docstring) — "FILM UNIT never logs in to, drives or
  scrapes a web app… The User opens the service in their own browser, copies a
  prompt out… brings the result back by pasting text or dropping files."
- `docs/BROWSER_HANDOFF.md:7` — "하지 않는 것: 로그인하지 않고, 대신 조작하지
  않고, 화면에서 결과를 긁어오지 않습니다."
- `docs/REDESIGN_W00.md` §6 결정 2 — 이 규칙을 2026-10-15에 재확인했다.
  2026-10-20 소유자 결정으로 번복되었다(§7 결정 1).
- `engine/execution_workers/subscription.py` — 구독 실행 환경 바인딩이
  존재하지만 routing이 `MANUAL`만 허용한다("a consumer site with no official
  execution API is MANUAL only — no UI automation"). 모든 route는
  qualification_state `UNQUALIFIED`.
- 유일한 실제 자동화는 `engine/conti_cli_driver.py`: `claude -p`(Claude Code CLI,
  사용자 본인 구독 로그인)로 judge/rewrite 요청에 답한다. 텍스트 단계 하나뿐이고,
  영상 생성·다운로드는 여전히 사람 손이다.
- `engine/openart_bridge.py` — OpenArt 연동이 존재하지만 소비자 UI 자동화가
  아니라 인증된 메타데이터·비용 호출 위의 **유료 요청/영수증 교환**이다
  (`register_quote`가 exact-config `model_cost` 증적을 등록하고,
  `import_completed`가 기록된 request/history ID가 일치할 때만 결과를
  가져온다). 크레딧 과금 경로이며 구독 자동화의 예외를 만들지 않는다.

### 1-4. 사람이 해야 하는 수동 단계의 현재 목록

한 곡을 끝내려면 지금 사용자가 해야 하는 일:

1. 가사 타이밍을 실제 음원을 들으며 **수작업으로** JSON에 적는다
   (`README.md:60` — "글자 수나 곡 길이로 보컬 시점을 추측하지 않습니다";
   ASR·강제 정렬은 미연결. `lyrics-align` 제안 경로는 있으나 faster-whisper
   로컬 설치 필요).
2. `conti-request` 출력을 복사 → 외부 LLM에 붙여넣기 → 답을 복사 →
   `conti-submit`. 샷당 반복(`engine/conti.py:400` `conti_request`).
3. `packets`/`conti-brief`의 프롬프트를 구독 앱에 붙여넣고, 생성하고,
   다운로드하고, 파일을 끌어놓는다 — **모든 샷에서**(`README.md:210-224`).
4. judge 요청 붙여넣기 → 답 붙여넣기 (`conti-judge-request/-submit`),
   FAIL/UNSURE는 사람이 verdict.
5. 파일 이름에 샷 ID가 없으면 `conti-batch assign --map`으로 수동 매핑
   (`README.md:262`).
6. LOCK, 검수 기록, `compile-final` 승인.

`python -m engine.cli` 명령이 60개 이상(`engine/cli.py`의 add_parser 목록).
비전문가가 이 체계를 통과할 가능성은 사실상 없다.

### 1-5. UI가 비전문가용이 아니다

- `app/control_panel.py:79-87` — 탭이 최대 **16개**: `00 · AI 모델`,
  `00 · 웹사이트 연결`, `01 · 제작 준비`, `01 · PROJECT`, `02 · DIRECTOR`,
  `03 · STORYBOARD`, `04 · LYRICS`, `05 · TIMELINE`, `06 · COMPILE / BUILDS`,
  `07 · RENDER · 고급`, `08 · ANIMATION`, `09 · 자산`, `10 · 컷 작업대`,
  `11 · 가사 검토`, `12 · 검수 대조`, `13 · 작업실`.
- 노출된 개념: LOCK, take kind(draft/final), build 번호, gate, attempt cap,
  estimate approval, renderer 선택, migration. 전부 제작 도구의 내부 용어다.
- 어디에도 "곡을 넣으면 끝까지 안내하는" 단일 흐름이 없다. 각 탭이 독립 도구이며
  순서는 README를 읽어야 안다.

### 1-6. 그래도 버리면 안 되는 자산

- 컴파일·편집 코어: `engine/compiler.py`, `engine/builds.py`, replay —
  불변 빌드, sha256 인벤토리, 검증은 실제로 작동(V03 보고).
- 가사 타이밍 독립성: `engine/lyrics.py` `timing_fingerprint`는 cue만 해시하고
  `engine/timeline.py`의 컷 편집은 cue를 움직이지 않는다.
- 샷 교체: `import-asset`, `split-shot`, `merge-shots`, `move-cut`, `snap-cut`.
- W00 콘티 자산: `engine/conti.py`(스키마·검증·READY 판정),
  `engine/conti_brief.py`(첫 프레임+모션 프롬프트, 4/6/8초 스냅),
  `engine/conti_batch.py`(sitting 묶음과 다운로드 배정),
  `engine/conti_cli_driver.py`(구독 CLI 드라이버 — 이미 no-API-key 자동화의
  존재 증명).
- 실패 의미론: UNKNOWN fencing, job identity, no-resubmit
  (`engine/segment_gen.py` docstring, `engine/budget.py`) — 유료 API에서
  만들어졌지만 "구독 앱을 다시 제출해 이중 차감"을 막는 데 정확히 필요한 것.
- 가져오기 검증: `engine/handoff.py`의 파일 검증·`handoff_log.json` 기록은
  자동화된 다운로드 결과물에도 그대로 쓸 수 있다.

**결론**: 문제는 편집기가 아니라 "생성을 실제로 일으키는 손"이 없다는 것과,
그 손을 요구하는 UI가 전문가용이라는 것이다.

---

## 2. 비전문가 목표 사용자 흐름 (화면 단위)

원칙: 사용자는 **7개 화면(0–6)**만 본다. 탭·LOCK·take·build 같은 내부 용어는 화면에
나오지 않는다(기록은 내부에서 계속 쓴다). 각 화면은 "지금 할 일" 하나만 묻는다.

### 화면 0 — 시작 / 로그인 상태 확인

- 앱(또는 로컬 웹 주소)을 열면 "연결된 구독 서비스" 카드 목록:
  각 서비스 옆에 `연결됨 / 브라우저에서 로그인 필요 / 확인 중`.
- 사용자가 하는 일: 로그인이 필요한 서비스의 **로그인하기** 버튼 → 자기
  브라우저에서 자기 계정으로 직접 로그인(앱은 비밀번호를 보지 않는다).
- 앱이 하는 일: helper가 각 서비스 탭을 열어 로그인 상태만 probe
  (결과만 기록, 쿠키·세션은 사용자 브라우저 프로필 안에만 존재).

### 화면 1 — 곡과 자료 올리기

- 한 화면에 세 입력: **노래 파일**(MP3/WAV), **가사**(텍스트 붙여넣기),
  **캐릭터 그림**(기존에 그린 2D 캐릭터 이미지 여러 장 끌어놓기).
- "이 영상의 느낌" 자유 서술 칸 하나.
- **시작하기** → 앱이 음원 분석(비트·섹션)과 샷 뼈대를 자동 생성하고,
  가사 타이밍 제안(정렬 제안 → 사용자가 화면 2에서 듣고 고치는 단계 포함)을
  준비한다. 기존 `init`/`analyze`/`package`/`lyrics-prepare`의 자동 연결.

### 화면 2 — 콘티 대화 (핵심 화면)

- 왼쪽: 가사 줄과 샷 목록이 시간축 위에 나란히. 오른쪽: LLM과의 채팅.
- LLM이 **한 번에 하나씩** 묻는다: "이 구절('…' 가사)에서 주인공은 뭘 하고
  있나요?", "카메라는 가만히 있을까요, 다가갈까요?" 사용자가 모르면
  **추천 보기** 버튼이 제안 2~3개를 보여 주고 택하게 한다.
- 답이 쌓이면 해당 샷 카드에 `장면 목표 / 인물 동작 / 카메라 / 조명 / 검사
  기준`이 채워진다. 내부적으로는 기존 `conti` 스키마(`engine/conti.py`
  `validate_conti`)와 `open_questions` → READY 판정을 그대로 쓴다.
- 뒷단 구현: LLM 표면은 (a) 구현된 `claude_cli` 드라이버, (b) 자동화된 구독
  웹챗(§3), (c) 수동 "지시문 복사/답 붙여넣기" — 세 가지 모두 같은
  request/answer 프로토콜을 쓰므로 UX가 동일하다.
- 완료 조건: 모든 샷 READY → 화면 3으로. 사용자는 "나중에 이어서 하기"로
  언제든 나갔다 올 수 있다.

### 화면 3 — 룩 확정

- 캐릭터 참조 시트(사용자가 올린 그림 기반으로 구독 이미지 서비스에서 만든
  정리본)와 각 샷의 **첫 프레임**을 격자로 보여 준다.
- 사용자가 하는 일: 마음에 드는지 샷별로 **좋아요 / 다시**만 누른다.
  "다시"에는 한 줄 사유("표정이 이상해요").
- 첫 프레임은 최종 품질 대부분을 결정하므로 여기서 승인된 것만 생성으로
  넘어간다(W00의 동일 판단 유지).

### 화면 4 — 생성 진행 보드

- 샷 카드 목록 + 상태: `대기 / 만들어지는 중 / 가져오는 중 / 검사 중 / 완료 /
  손봐야 함`.
- 사용자는 기본적으로 **보기만** 한다. 자동화가 막히면 해당 샷 카드에
  "직접 만들기" 버튼이 뜨고(수동 fallback: 프롬프트 복사 + 파일 끌어놓기),
  진행은 멈추지 않는다.
- 구독 한도 소진 시 "내일/리셋 후 이어서 합니다"로 표시. 대기 중에도 앱을
  꺼도 되고 큐 상태는 프로젝트 폴더에 남는다.

### 화면 5 — 보고 고치기

- Preview(곡 전체 MP4) 재생 + 샷별 건너뛰기.
- 마음에 안 드는 샷에서 **이 샷 다시 만들기** → 무엇이 다른지 한 줄 →
  내부에서 `conti-rewrite-request`류의 수정 루프로 새 프롬프트를 만들고 다음
  take를 attempt n+1로 큐에 넣는다(샷당 상한은 기존 `max_attempts_per_shot`
  정책 계승).
- 다른 샷·가사 타이밍·이전 빌드는 건드리지 않는다.

### 화면 6 — 완성

- 가사 자막이 실제로 노래와 맞는지 미리보기로 확인하는 화면(글꼴·타이밍
  검증 경고는 여기에만 사람 언어로 표시).
- **완성하기** → Final 조건 검사를 통과하면 `MASTER_SUBBED.mp4`와
  `MASTER_CLEAN.mp4`를 만든다. 조건이 안 맞으면 무엇이 빠졌는지만 보여 준다.
- 발행(YouTube 업로드 등)은 앱이 하지 않는다 — §6.

목표 지표: 비전문가가 화면 1→6을 **설명서 없이** 통과하고, 자동화가 전부 실패
해도 수동 fallback만으로 같은 경로를 끝낼 수 있어야 한다.

---

## 3. API 키 없는 구독 자동화 아키텍처

### 3-1. 어떤 서비스를 어떻게 돌리는가 (§7 승인 목록 안의 후보 — 실제 UI 조사 필요)

| 역할 | 후보 서비스 | 구동 방식 (후보) |
|---|---|---|
| 콘티·프롬프트 글 | Claude Code CLI(구현됨, `engine/conti_cli_driver.py`), ChatGPT/Codex CLI, Gemini CLI | 벤더 공식 CLI — 자동화 가능, ToS 리스크 낮음 |
| 콘티·프롬프트 글 | ChatGPT 웹, Claude 웹, Gemini 웹, Grok 웹 | 브라우저 자동화(사용자 프로필) — 소비자 UI, ToS 리스크 있음 |
| 참조 이미지·첫 프레임 | Gemini 앱(Nano Banana), ChatGPT 이미지, Midjourney 웹, OpenArt 웹, Grok Imagine | 브라우저 자동화 — 후보, 미검증 |
| 샷 영상 | ChatGPT/Sora, Google Flow(Veo), Gemini 앱의 Veo, Grok Imagine(SuperGrok), Kling, Runway, OpenArt 영상, Midjourney video | 브라우저 자동화 — §7 승인 목록과 일치. **각 서비스 UI가 batch/업로드/다운로드를 어떻게 노출하는지 확인이 선행 과제** |

선정 원칙: (a) JunTae가 실제로 내고 있는 구독 우선, (b) 첫 프레임+참조 이미지
업로드를 받는 것 우선(2D 캐릭터 일관성), (c) 구독 단위가 예측 가능한 것.
자동화가 허용된 서비스는 §7 승인 목록(ChatGPT/Sora, Flow/Gemini Veo,
Grok Imagine, Kling, Runway, OpenArt, Midjourney)뿐이고, 표의 후보 중
목록 밖 항목(예: Claude 웹, Gemini 웹 텍스트)은 수동 모드로만 쓴다.

### 3-2. 세션·로그인 모델 — 자격은 사용자 기기를 떠나지 않는다

- 사용자는 **자기 브라우저 프로필에서 직접 로그인**한다(각 서비스 첫 연결 때
  한 번). 앱은 비밀번호·쿠키·토큰을 저장·전송·복사하지 않는다. 앱이 기록하는
  것은 `service: flow, logged_in: true, checked_at: …` 같은 probe 결과뿐.
- helper가 브라우저를 붙이는 방식(§7 결정: **A 채택**, B extension은 후속 보류):
  - **A. Playwright가 사용자 Chrome/Edge 프로필에 attach**:
    `chrome --remote-debugging-port`로 띄운 사용자 프로필에
    `connect_over_cdp`, 또는 `launch_persistent_context(user_data_dir=…)`로
    전용 프로필 복사본이 아닌 **사용자 지정 프로필**을 연다. 로그인 상태는
    프로필 안에 그대로 남는다.
  - **B. (보류) 브라우저 extension**이 사용자가 연 탭 안에서만 동작하고, 로컬
    localhost의 앱과 메시지를 주고받는다. 셀렉터 변경에 더 강하고
    "사람이 보고 있는 탭에서 일어나는 일"이라는 모델이 명확.
- 어느 쪽이든 **headless는 금지**: 자동화는 항상 사용자가 볼 수 있는 창에서
  일어난다(리스크 완화이자 디버깅 경로).
- 2FA·CAPTCHA·추가 동의 화면이 뜨면 job을 `NEEDS_HUMAN`으로 두고 사용자에게
  넘긴다 — 우회하지 않는다.

### 3-3. web app / 로컬 helper 분할

```
┌──────────────────────┐         ┌─────────────────────────┐
│ Web app (localhost)  │         │ Local helper (Windows 1순위) │
│ - 7개 화면 UX         │  파일   │ - PowerShell bootstrapper   │
│ - 콘티 대화 상태      │  큐     │ - Playwright(프로필 attach)  │
│ - 프로젝트 상태·기록   │◄──────►│ - 서비스별 adapter(셀렉터)   │
│ - job queue writer   │  JSON  │ - 업로드/생성/다운로드 실행   │
│ - ffmpeg 편집(기존)   │         │ - 다운로드 폴더 → inbox 이동  │
└──────────────────────┘         └─────────────────────────┘
```

- **web app**: 기존 로컬 Streamlit을 유지한 채(§7 결정: Streamlit 유지)
  위 7개 화면(0–6) 단일 흐름으로 재구성. 프로젝트 상태·검수·빌드는 기존
  `engine` 모듈을 그대로 호출.
  ffmpeg 편집·자막·빌드는 `engine/compiler.py` 계열 그대로 — 서버리스 이전 없음.
- **helper**: Windows가 1순위(§7 결정: Windows first, PowerShell bootstrap).
  `bootstrap.ps1`이 Python venv + `playwright install` + 서비스 adapter를
  준비하고, helper 프로세스가 프로젝트 폴더의 큐를 감시한다. macOS/Linux는 같은
  프로토콜의 나중 포트.
- **job queue 프로토콜**(파일 기반, 네트워크 불필요 — 프로젝트 폴더 공유):

```
automation/jobs/<job_id>.json      # 앱이 씀 → helper가 읽고 상태 갱신
automation/results/<job_id>/       # helper가 씀 → 앱이 import
automation/helper.log              # helper 자신의 실행 로그(민감정보 금지)
```

job 문서 예시:

```json
{
  "job_id": "J-20261019-0007",
  "idempotency_key": "sha256(project_id|shot_id|attempt|prompt_digest|service)",
  "kind": "video_generate",
  "service": "flow",
  "shot_id": "S012",
  "attempt": 2,
  "prompt": "…",
  "attachments": ["bible/refs/char01.png", "frames/S012_a1.png"],
  "expected": {"min_seconds": 6, "aspect": "16:9"},
  "state": "QUEUED",
  "created_at": "…"
}
```

state 전이: `QUEUED → CLAIMED(helper) → SUBMITTED → DOWNLOADED → IMPORTED /
FAILED / NEEDS_HUMAN / UNKNOWN`. `SUBMITTED`인데 결과가 안 오면 `UNKNOWN`으로
fence — **같은 idempotency_key의 재제출만 허용되고, 새 key의 "같아 보이는"
재제출은 차단**(기존 segment_gen의 UNKNOWN 규칙 계승). `IMPORTED`된 샷은 절대
재제출되지 않는다(§4 이중 차감 방지).

- **다운로드 → 가져오기**: helper가 받은 파일을 `automation/results/`에 두면
  앱이 기존 `handoff.import` 계열 검증(형식·길이·해상도·handoff_log 기록)과
  `conti-check`를 그대로 태운다. 자동화가 만든 파일도 사람이 끌어놓은 파일과
  **정확히 같은 검증 경로**를 지나는 것이 원칙.

### 3-4. 위험 — 숨기지 않고 명시한다

| 위험 | 정직한 평가 | 완화 |
|---|---|---|
| ToS 위반·계정 정지 | OpenAI·Anthropic·xAI 등의 소비자 약관은 비공식 자동화를 제한한다(`docs/BROWSER_HANDOFF.md:7`이 이를 이유로 자동화를 거부했었다). **계정 정지는 실제로 가능한 결과다.** | 서비스별 명시적 사용자 동의 후에만 자동화; 공식 CLI가 있는 단계는 CLI 우선; 사람 속도 pacing(한 번에 하나, 요청 간격); headless 금지; 사용자 로그인 세션만 사용. 위험을 감수할지는 서비스별로 JunTae/사용자가 결정. |
| UI 변경으로 adapter 파손 | 소비자 웹 UI는 예고 없이 바뀐다. 셀렉터 의존 자동화는 깨진다. | 서비스별 adapter에 버전·health probe: job 제출 전 "이 페이지가 알고 있는 형태인지" 검사하고, 모르는 형태면 submit하지 않고 `FAILED_UNSUPPORTED_UI` → 수동 fallback으로 자동 강등. |
| Rate limit·한도 | 구독 한도는 계정·시기마다 다르고 측정된 수치가 없다. | 큐는 한 번에 하나; 한도 신호 감지 시 `WAIT_QUOTA`로 대기(재시도 폭풍 금지); 실제 소비량은 `conti-metrics`류 장부에 기록해 추정값 축적. |
| 이중 차감/중복 제출 | 같은 프롬프트를 두 번 제출하면 구독 크레딧을 두 번 쓴다. | idempotency_key + UNKNOWN fencing(위). helper가 죽어도 큐 상태로 복구; 모호하면 새 제출 대신 reconcile. |
| 로그인 세션 만료 | 세션은 만료한다. | probe 실패 → `NEEDS_HUMAN`(재로그인 버튼). 재로그인은 항상 사용자. |
| 프롬프트/자산 유출 | 프롬프트와 캐릭터 그림이 서비스 서버로 간다(구독 사용의 본질). | 보내는 첨부 목록을 job에 명시·기록; 프로젝트 밖 경로는 거부(기존 handoff 규칙). |

**수동 fallback은 아키텍처의 일부이지 예외가 아니다**: 모든 job 화면에
"직접 만들기" 모드가 있고, 그 모드는 기존 `handoff.py`의 복사+끌어놓기 경로를
그대로 쓴다. 자동화 adapter가 전부 죽어도 제품은 느려질 뿐 멈추지 않는다.

---

## 4. 유지할 원칙

1. **2D 자체 캐릭터 스타일 일관성**: 사용자가 올린 캐릭터 그림 + 승인된 참조
   시트가 모든 샷의 조건 입력이다. `bible/characters.yaml`의
   `reference_images`와 "승인된 첫 프레임이 곧 image-to-video의 첫 프레임"
   규칙(`docs/OPENART_BRIDGE.md:9`)을 유지. 프롬프트만으로 인물을 묘사하는
   방식(무료 이미지 경로의 한계, `README.md:155`)으로 되돌리지 않는다.
2. **원곡·가사 타이밍 불변**: 컷 편집은 cue를 움직이지 않는다
   (`engine/lyrics.py` `timing_fingerprint`). 최종 MP4는 원곡만 AAC로 싣고
   생성 클립의 소리는 버린다(기존 동작).
3. **샷 단위 교체와 불변 빌드**: 한 샷을 갈아도 나머지 샷·음원·가사·이전
   빌드의 바이트가 유지된다(`engine/builds.py`, V03 회귀).
4. **예산 가드 / 이중 차감 금지**: 모든 생성 시도는 idempotency_key로 기록되고,
   완료된 샷은 재제출하지 않는다. 구독 단위·시도 횟수·사람 시간은 장부
   (`qc/conti/`)에 별도 단위로 남긴다 — 돈으로 환산해 합치지 않는다.
   기본 경로에 paid API는 없다(legacy 코드는 다음 항목의 게이트 뒤에만
   남는다).
5. **유료 API 없음**: `engine/providers.py`의 paid provider(gemini_video,
   fal_video, paid text)는 기본 제품에서 선택지로 보이지 않게 하고, 키 입력
   UI를 제거한다. API 키 자동화 경로를 만들지 않는다. (W00 §4의
   `FILM_UNIT_LEGACY_PAID` 격리 방식 계승.)

---

## 5. 노드별 구현 계획 (`.aiops/program.json`에 붙여넣기 가능한 형식)

설계 순서: **w01-001~003까지가 수동 fallback으로 end-to-end MP4를 만드는
첫 뼈대**이고, w01-004 이후가 자동화다. 각 노드는 PR 하나 크기를 목표로 한다.

```json
[
  {
    "id": "w01-001",
    "title": "W01-001: 단일 흐름 web UI 뼈대 (7개 화면 0–6, 기존 engine 호출)",
    "depends_on": [],
    "spec": "목표: app/ 아래에 새 단일 흐름 UI(화면 0~6, 총 7개 껍데기 + 실제 라우팅)를 만든다. 기존 16탭 control_panel은 유지하되 진입점을 새 흐름으로 바꾼다.\n범위: 화면 1 업로드 → 기존 init/analyze/package 호출; 화면 4 진행 보드는 수동 모드만; 화면 6은 기존 compile-preview 결과 재생. 콘티·자동화는 다음 노드.\n완료 기준: 새 UI에서 곡+가사+이미지를 넣어 프로젝트가 만들어지고 기존 placeholder Preview MP4가 화면 6에서 재생된다. 기존 pytest 통과.\n공통 조건: 유료 요청·credential 발급·실제 서비스 호출 없음. 기존 프로젝트/검수/LOCK 계약 유지.",
    "audit_floor": "A2",
    "astra_auto_merge": true
  },
  {
    "id": "w01-002",
    "title": "W01-002: 가이드 콘티 대화 (샷 단위 Q&A → conti 스키마 저장)",
    "depends_on": ["w01-001"],
    "spec": "목표: 화면 2를 실제로 만든다. LLM 표면은 우선 수동(지시문 복사/답 붙여넣기) + 구현된 claude_cli 드라이버 두 가지로, 한 번에 하나의 질문을 만들고 답을 기존 conti 스키마로 검증·저장한다.\n범위: 질문 생성기(샷 manifest + timed cues + beats + bible → 한 질문), 답 파서 → validate_conti, open_questions → READY, 진행률 표시, '추천 보기' 제안 경로.\n완료 기준: 대화만으로 한 샷이 READY가 되고, 잘못된 답은 무엇이 비었는지 사람 언어로 표시된다. 가사 타이밍·컷·검수·LOCK을 바꾸지 않는다. 테스트로 검증.\n공통 조건: LLM 자동화는 기존 claude_cli 경로 재사용만. 새 서비스 자동화 없음.",
    "audit_floor": "A2",
    "astra_auto_merge": true
  },
  {
    "id": "w01-003",
    "title": "W01-003: 수동 fallback으로 end-to-end 실제 MP4 (샷 1개 이상 실제 테이크)",
    "depends_on": ["w01-002"],
    "spec": "목표: 자동화 없이, 새 UX의 화면 3(룩 승인) → 화면 4('직접 만들기' 수동 모드: 프롬프트 복사 + 파일 끌어놓기) → 화면 5(Preview+샷 다시 만들기) → 화면 6(Final 조건 검사)을 관통해, 실제 구독 앱에서 사람이 만든 테이크 1개 이상이 들어간 실곡 MP4를 만든다.\n범위: 기존 handoff import/conti-brief/conti-check/compile-preview/compile-final을 새 화면 뒤에 연결. '이 샷 다시'는 기존 conti-revision 루프.\n완료 기준: docs/evidence/w01-e2e-manual/ 에 실제 생성→가져오기→검사→Preview 빌드 기록(시도·단위·사람 시간 포함). 이 노드의 실곡·실제 서비스 작업은 사람이 수행하고 증거만 코드가 검증한다.\n공통 조건: 소비자 앱 자동 조작 없음(수동 fallback만). 유료 요청 없음.",
    "audit_floor": "A3",
    "astra_auto_merge": false,
    "user_merge": true,
    "astra_gate": "ARCHITECTURE"
  },
  {
    "id": "w01-004",
    "title": "W01-004: 자동화 job queue + idempotency 장부 (파일 프로토콜)",
    "depends_on": ["w01-003"],
    "spec": "목표: §3-3의 automation/jobs·results 파일 큐와 idempotency_key, 상태 전이, UNKNOWN fence, 'IMPORTED 샷 재제출 금지'를 구현한다. helper 없이도 큐를 쓰고 수동 모드가 같은 큐의 한 state로 표현된다.\n범위: job schema + writer + 상태 기계 + 장부 + 수동 모드가 큐를 통과하도록 화면 4 재배선.\n완료 기준: 동일 idempotency_key 재제출이 차단되고, helper 부재 시 모든 job이 '직접 만들기'로 표시되는 것을 테스트로 검증.\n공통 조건: 실제 서비스 호출 없음.",
    "audit_floor": "A2",
    "astra_auto_merge": true
  },
  {
    "id": "w01-005",
    "title": "W01-005: Windows helper bootstrap + 세션 probe (제출 없음)",
    "depends_on": ["w01-004"],
    "spec": "목표: PowerShell bootstrapper + helper 프로세스가 사용자 본인 Chrome/Edge 프로필에 Playwright attach(§7 결정 — 전용 프로필 복사본이 아닌 사용자 지정 프로필)로 각 후보 서비스의 로그인 상태만 probe한다. 제출·업로드·스크래핑은 하지 않는다.\n범위: bootstrap.ps1, helper 실행기, probe adapter 1개(구조만), 화면 0의 '연결됨/로그인 필요' 표시 연결.\n완료 기준: 로그인된 프로필에서 logged_in:true/false가 화면 0에 표시된다. 자격·쿠키를 파일에 쓰지 않는 것을 코드 리뷰로 확인.\n공통 조건: 생성 요청 없음. Windows 우선, 다른 OS는 후속.",
    "audit_floor": "A3",
    "astra_auto_merge": false,
    "user_merge": true,
    "astra_gate": "ARCHITECTURE"
  },
  {
    "id": "w01-006",
    "title": "W01-006: 첫 서비스 adapter — 샷 1개 자동 생성 (사용자 확인 제출)",
    "depends_on": ["w01-005"],
    "spec": "목표: §7 승인 목록(ChatGPT/Sora, Flow/Gemini Veo, Grok Imagine, Kling, Runway, OpenArt, Midjourney) 안에서 노드 시작 시 고른 첫 영상 서비스의 adapter로 첫 프레임 업로드 + 프롬프트 입력 + 생성 요청 + 다운로드 → results 폴더 → 기존 import/conti-check 경로까지 자동으로 간다. 첫 N회는 각 제출 전에 사용자가 브라우저에서 확인하는 모드로 시작한다.\n범위: adapter 1개, UI probe 규칙(모르는 페이지면 FAILED_UNSUPPORTED_UI → 수동 강등), pacing(한 번에 하나), NEEDS_HUMAN 경로.\n완료 기준: 샷 1개가 자동화로 생성·가져오기·검사되어 Preview에 들어간다. 실제 서비스 사용은 사람 승인 하에.\n공통 조건: 서비스 ToS 위험은 §3-4 표기대로 사용자 명시 동의가 선행. 유료 API 아님.",
    "audit_floor": "A3",
    "astra_auto_merge": false,
    "user_merge": true,
    "astra_gate": "ARCHITECTURE"
  },
  {
    "id": "w01-007",
    "title": "W01-007: 콘티 LLM 자동화 (승인 목록 내 구독 웹챗 adapter 추가)",
    "depends_on": ["w01-006"],
    "spec": "목표: 화면 2의 LLM 표면에 자동화 경로를 추가한다 — claude_cli(구현됨, §7에서 우선 표면으로 확정)에 더해 §7 승인 목록 안의 구독 웹챗(예: ChatGPT 웹) adapter로 지시문 전송·답 수신을 자동화한다.\n범위: webchat adapter 1개, 답 추출 → 기존 conti 검증, 실패 시 수동 붙여넣기 fallback.\n완료 기준: 한 샷의 콘티가 화면 2에서 수동 복사 없이 READY가 된다(테스트는 fake DOM, 실제 검증은 증거 기록).\n공통 조건: 유료 API 아님.",
    "audit_floor": "A2",
    "astra_auto_merge": true
  },
  {
    "id": "w01-008",
    "title": "W01-008: 배치 생성 + 한도 대기 (sitting 단위 자동화)",
    "depends_on": ["w01-006"],
    "spec": "목표: 기존 conti-batch의 sitting 개념을 자동화 큐에 연결해, sitting_limit 개씩 샷을 묶어 순차 제출하고 한도 신호(WAIT_QUOTA)에서 안전하게 대기한다.\n범위: 배치 스케줄러, 한도 감지 → 대기, 배치 진행률을 화면 4에 표시.\n완료 기준: sitting 하나(기본 4샷)가 개입 없이 QUEUED→IMPORTED까지 가고, 한도 신호에서 재시도 폭풍 없이 멈춘다.\n공통 조건: 유료 API 아님.",
    "audit_floor": "A2",
    "astra_auto_merge": true
  },
  {
    "id": "w01-009",
    "title": "W01-009: 수정 루프 자동 연결 ('이 샷 다시' → 재생성)",
    "depends_on": ["w01-007", "w01-008"],
    "spec": "목표: 화면 5의 '이 샷 다시'가 사유 한 줄 → LLM 콘티/프롬프트 재작성 → 새 job(attempt n+1, 새 idempotency_key) → 가져오기 → 검사까지 자동으로 잇는다. attempt cap은 기존 정책.\n완료 기준: 한 샷의 수정 한 바퀴가 사람의 사유 입력 외 개입 없이 완료된다.\n공통 조건: cap 도달 시 자동 중단, 상한 상향은 사람 결정.",
    "audit_floor": "A2",
    "astra_auto_merge": true
  },
  {
    "id": "w01-010",
    "title": "W01-010: Final 완성 경로를 새 UX에 연결 (가사 검토 + 승인)",
    "depends_on": ["w01-003"],
    "spec": "목표: 화면 6에서 가사 타이밍 검토·글꼴 검증·Final 조건을 사람 언어로 표시하고, 통과 시 기존 compile-final로 MASTER_CLEAN/SUBBED MP4를 만든다.\n완료 기준: Final 조건 미충족 항목이 무엇인지 표시되고, 충족 시 실제 Final MP4가 나온다.\n공통 조건: Final 승인은 항상 사람.",
    "audit_floor": "A2",
    "astra_auto_merge": true
  },
  {
    "id": "w01-011",
    "title": "W01-011: 유료 API provider 제거/격리 + 키 UI 삭제",
    "depends_on": ["w01-001"],
    "spec": "목표: providers.py의 paid provider(텍스트·이미지·영상)를 기본 선택지에서 제거하고 FILM_UNIT_LEGACY_PAID 게이트 뒤로 옮긴다. 모델 선택의 키 입력 폼을 기본 UI에서 없앤다.\n완료 기준: 기본 설치에서 API 키 입력 칸이 보이지 않고 paid provider로 job이 만들어지지 않는다. secrets.json의 기존 키는 삭제하지 않고 읽지도 않는다(사용자 파일 보존).\n공통 조건: 기존 테스트 중 paid 경로를 가정한 것은 게이트 하에서만 실행.",
    "audit_floor": "A2",
    "astra_auto_merge": true
  },
  {
    "id": "w01-012",
    "title": "W01-012: W01 수용 — 실곡 한 절 이상 자동화 경로로 완성 + 문서",
    "depends_on": ["w01-008", "w01-009", "w01-010", "w01-011"],
    "spec": "목표: 소유자가 추후 고르는 실제 곡(§7 TBD)의 한 절 이상을 화면 1→6으로 완성한다(콘티 대화 → 자동 생성 → 수정 루프 1회 이상 → Preview). 노드 내 자동 테스트는 짧은 placeholder/fixture 음원을 쓰고, 실곡 실행은 사람이 수행한다(§6). 장부(시도·단위·사람 시간)와 adapter 파손/수동 강등 사례를 docs/evidence/w01-first-verse/에 기록한다.\n완료 기준: 해당 구간이 실제 생성 테이크로 Preview에 들어가고, 준태님이 확인한다. 문서: README의 W01 절 + 각 adapter의 지원 UI 버전 표.\n공통 조건: 실제 서비스 사용·승인은 사람.",
    "audit_floor": "A3",
    "astra_auto_merge": false,
    "user_merge": true,
    "astra_gate": "MILESTONE"
  }
]
```

---

## 6. 사람이 해야 하는 일 (자동화해도 남는 것)

1. **로그인**: 각 구독 서비스 첫 연결 때 자기 브라우저에서 직접 로그인.
   재로그인·2FA·CAPTCHA도 항상 사람.
2. **ToS 수락**: 각 서비스의 이용약관과 "이 서비스에서 자동화를 허용할지"에
   대한 명시적 동의는 사용자의 결정이며, 동의 없는 서비스는 수동 모드로만 쓴다.
3. **콘티 대화**: LLM의 질문에 답하고 취향을 고르는 것 — 제품의 핵심 입력.
4. **룩 승인**: 캐릭터 참조 시트와 첫 프레임의 좋아요/다시.
5. **Final 승인**: 완성 MP4의 최종 확인과 승인(사람 서명).
6. **발행**: YouTube 등 업로드·공개는 앱이 하지 않는다.
7. **한도·차단 대응**: 구독 한도 소진 시 기다릴지 다른 서비스로 갈지 결정;
   계정 경고가 오면 자동화 중단 여부를 사람이 결정.
8. **첫 실곡 투입**: 자동 테스트는 짧은 placeholder/fixture 음원만 쓴다.
   실제 곡의 선정(소유자 TBD, §7)과 첫 실곡 실행은 항상 사람 단계다.

---

## 7. 소유자 결정 — 결정 완료 (2026-10-20)

JunTae가 2026-10-20 11:57 KST에 전부 확정했다. 이전 "결정 필요" 항목의 처리 결과:

1. **브라우저 자동화 승인 — W00 §6 결정 2 번복 확정.** 사용자 본인이 내고 있는
   구독의 브라우저 자동화를 **승인한다**. 허용 서비스: **ChatGPT/Sora,
   Flow/Gemini Veo, Grok Imagine, Kling, Runway, OpenArt, Midjourney**.
   이 목록 밖의 서비스를 자동화하려면 새 승인이 필요하다. ToS 위반·계정 정지
   리스크(§3-4)와 서비스별 사용자 명시 동의 조건은 그대로 유효하다.
2. **OS — Windows first.** bootstrap은 PowerShell로 시작하고, macOS/Linux는
   같은 프로토콜의 후속 포트다(§3-3).
3. **helper 방식 — Playwright attach.** 사용자 본인 브라우저 프로필에
   Playwright를 붙인다(§3-2 안 A). 브라우저 extension은 이번 범위에 넣지
   않는다(필요 시 후속 결정).
4. **web UI — Streamlit 유지.** 기존 로컬 Streamlit을 7개 화면(0–6) 단일
   흐름으로 재구성한다. 별도 웹 스택으로 교체하지 않는다.
5. **콘티 LLM — claude_cli 우선.** 화면 2의 첫 자동화 표면은 구현된
   `engine/conti_cli_driver.py` 경로다. 구독 웹챗 adapter는 후속 노드
   (w01-007)에서 승인 목록 안 서비스로 추가한다.
6. **기존 paid 경로 — `FILM_UNIT_LEGACY_PAID` 게이트 유지, 기본 OFF.**
   코드는 보존하되 기본 설치에서는 비활성화한다(w01-011).
7. **첫 실곡 — 소유자가 추후 결정(TBD).** 그 전까지 모든 자동 테스트는
   짧은 placeholder/fixture 음원을 쓰고, 첫 실곡 실행은 사람 단계로
   남는다(§6 항목 8).
