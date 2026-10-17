# AI Office (본체)

GitHub 이슈에 `ai-office` 라벨을 붙이면 AI 여러 개가 역할을 나눠 일하고 PR을 만든다.

```
이슈 + 라벨 → 기획(Planner) → 구현(Builder, 작업마다) → 정리(post_build) → 테스트(CI) → 리뷰(Reviewer) → PR
                    ↑ 막는 질문이 있으면 이슈에 묻고 멈춤                      PR에 "/ai-office fix" → 수정 라운드
```

| 역할 | 기본 엔진 | 하는 일 |
|--|--|--|
| Planner | Claude (`claude -p`) | 이슈를 작업 1~N개로 쪼갬, 모르면 질문 |
| Builder | Cursor (`cursor-agent -p`) | 작업마다 파일 수정, 작업마다 커밋 |
| Reviewer | Claude 또는 **Gemini** (API 직접 호출) | diff·테스트 결과 검토 → `APPROVE` / `REQUEST_CHANGES` |
| Fixer | (Builder 엔진) | PR 댓글의 수정 요청 반영 |

엔진은 레포마다 `office/config.yaml`에서 고른다. 엔진이 실패하면(키 없음, 한도) `fallback_engine`으로 대신 실행한다.

## 구조: 본체는 여기 한 곳, 레포는 설정만

```
ai-office (이 레포, 공개)                   각 레포
├─ office/orchestrator.py   ← 본체          ├─ .github/workflows/ai-office.yml  ← templates/ai-office.yml 복사 (12줄짜리 호출)
├─ office/roles/*.md        ← 기본 지시문   └─ office/config.yaml               ← 엔진·설치·테스트·규칙
├─ .github/workflows/office.yml ← 재사용 워크플로     office/roles/*.md (선택)      ← 지시문 덮어쓰기
└─ templates/ai-office.yml
```

- 본체를 고치면 모든 레포가 다음 실행부터 새 버전을 쓴다. 고정하려면 caller에서 `@main` 대신 태그(`@v5`).
- **이 레포는 공개여야 한다.** 공개 레포(2027_F1tenth_test)는 비공개 레포의 재사용 워크플로를 부를 수 없다.
  비밀 정보는 여기 없다 (키는 각 레포 Secrets).

## 새 레포에 붙이기

1. `templates/ai-office.yml` → 그 레포 `.github/workflows/ai-office.yml` (기본 브랜치에 있어야 이슈 트리거가 동작)
2. `office/config.yaml` 작성 (아래)
3. 레포 Settings → Secrets → Actions: 쓰는 엔진의 키만
   - `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`, Pro/Max 구독) 또는 `ANTHROPIC_API_KEY`
   - `CURSOR_API_KEY` (cursor.com 대시보드 → API Keys)
   - `GEMINI_API_KEY` (aistudio.google.com → Get API key)
4. Settings → Actions → General → Workflow permissions: **Read and write** + **Allow GitHub Actions to create and approve pull requests**

## config.yaml

```yaml
max_tasks: 3
max_diff_chars: 60000
gemini_allowed: true          # false면 gemini 역할이 있을 때 시작부터 중단 (개인정보 레포용)
base_branch: ""               # 비우면 기본 브랜치에서 작업
setup_commands: []            # 실행 전 준비 (apt/pip 설치, 자료 추출…). 하나라도 실패하면 중단
test_commands: []             # 오케스트레이터가 직접 돌리는 테스트
test_discover: false          # true: 패키지 안 tests.py → python -m 패키지.tests 자동 추가
test_exclude: []
post_build_commands: []       # Builder 직후·커밋 전 자동 정리 (포맷터 등)
max_fix_rounds: 2
project_notes: |              # 모든 역할 프롬프트에 들어가는 레포 규칙
  ...
roles:
  planner:  {engine: claude, model: sonnet, max_turns: 8, allowed_tools: [Read, Grep, Glob], timeout_sec: 600}
  builder:  {engine: cursor, model: "", extra_args: ["--force", "--trust"], timeout_sec: 1500}
  reviewer:
    engine: gemini
    timeout_sec: 900
    fallback_engine: claude   # gemini가 실패하면
    fallback: {model: sonnet, max_turns: 10, allowed_tools: [Read, Grep, Glob]}
  # 수정 라운드(Fixer 지시문)는 builder 엔진 설정으로 실행
```

## Gemini를 쓸 때 주의

- **AI Studio API 키(`GEMINI_API_KEY`)로 Gemini API를 직접 호출**한다 (Gemini CLI 안 씀).
  Gemini CLI는 2026-06-18부터 무료 등급·Google AI Pro/Ultra 구독 요청을 받지 않는다 (유료 API 키만).
  → **Gemini 앱 구독(AI Pro)은 여기서 쓸 수 없다.** 무료 또는 결제한 AI Studio 키가 필요하다.
- 도구가 없어 **글만 내는 역할(reviewer, planner)에만** 쓴다. diff는 프롬프트에 들어간다. builder에는 못 쓴다 (시작 시 막음).
- 무료 한도는 **모델별 하루 요청 수** (예: gemini-3.8-flash 하루 20회, 2026-10 실측). 429(한도)면 바로 `fallback_engine`으로.
  기본 모델 `gemini-flash-latest`, 바꾸려면 role의 `model`.
- **무료 등급 약관**: 입력·출력이 서비스 개선에 쓰이고 사람 검토자가 볼 수 있다. 개인정보·기밀을 보내지 말 것.
  → 자소서·개인정보 레포는 `gemini_allowed: false` (키 프로젝트에 결제를 켜 유료 등급이면 해당 없음).

## 로컬에서 돌려 보기

```bash
cd 대상레포
AI_OFFICE_REPO_ROOT=$PWD python /경로/ai-office/office/orchestrator.py --issue-title "..." --issue-body "..." --no-push
python orchestrator.py --needs-engine gemini   # 이 레포가 gemini를 쓰면 exit 0
python orchestrator.py --hook setup            # setup_commands만 실행
```

## 동작 메모

- 프롬프트가 100 KB를 넘으면 본문을 표준입력으로 넘긴다 (리눅스 인자 하나 128 KB 제한).
- 사람 확인이 필요한 PR(리뷰 반려·테스트 실패·작업 실패)은 Draft + `needs-human` 라벨.
- 수정 라운드(`/ai-office fix …`)는 소유자·협업자 댓글만. 머지된 PR이면 후속 PR을 만든다.
- PR 본문에 역할별 사용량 표 (Cursor CLI가 토큰 수를 안 주면 `-`).
