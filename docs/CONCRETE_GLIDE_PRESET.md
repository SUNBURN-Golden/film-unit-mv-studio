# Concrete Glide preset

`concrete_glide`는 《철콘 근크리트》(Studio 4°C)의 **도시 밀도, 군청빛 밤, 공간을 활공하는 카메라 언어**에서 영감을 받습니다. 원작 캐릭터·지명·컷을 옮기거나 원작 프레임을 에셋으로 사용하지 않습니다. Gunjo City, 성인 길잡이 Mira와 견습 Tov, 도시를 내려가며 길을 찾다가 견습이 귀갓길을 이끄는 이야기는 이 preset의 오리지널 초안입니다.

회색 종이·이분할·고정 카메라 중심인 `water_please`와 달리, 콘크리트 계단과 옥상, 전경의 전선·기둥, 먼 창문 불빛으로 깊이를 만듭니다. 군청/남색이 도시를 채우고 호박색 창문과 소량의 산호색 간판이 시선을 이끕니다. 머리와 복장 실루엣, 물탑·고가 랜드마크를 샷 사이에 유지합니다. 읽을 수 없는 글자 장식은 사용하지 않습니다.

## 새 프로젝트에 적용

```bash
python -m engine.cli init gunjo_city --audio /path/master.mp3 --brief /path/brief.md --lyrics /path/lyrics.txt
python -m engine.cli analyze projects/gunjo_city
python -m engine.cli package projects/gunjo_city --preset concrete_glide
python -m engine.cli compile-preview projects/gunjo_city
python -m engine.cli builds projects/gunjo_city
```

`package`는 `presets/concrete_glide/`의 네 YAML을 읽어 `bible/style_bible.yaml`, `characters.yaml`, `locations.yaml`, `directing.yaml`을 기록합니다. 이미 패키지가 있는 프로젝트에는 덮어쓰지 않으므로 새 프로젝트 ID를 사용하세요. 위 기본 경로의 콘티는 타이밍 슬레이트입니다. 제작용 그림은 별도로 검토하여 가져옵니다. 가사 원문만 넣었다면 자막 타이밍은 아직 미완료이며, README의 `lyrics-prepare`/`lyrics-import` 절차로 실제 보컬과 대조합니다.

## 240초 로컬 애니마틱

저장소 루트에서 FFmpeg/ffprobe가 설치된 Python 환경으로 실행합니다.

```bash
python -m pip install -e '.[test]'
python -m examples.concrete_glide_demo --root projects --name concrete_glide_demo --seconds 240
python -m engine.cli compile-preview projects/concrete_glide_demo --quality final
python -m engine.cli builds projects/concrete_glide_demo
```

예제는 합성 테스트 음원을 분석한 실제 ms 타임라인 위에 도시 경로와 로컬 도형 콘티/초안 영상을 배치합니다. `init → analyze → package --preset concrete_glide`와 같은 제작 경로를 사용하며 유료 영상 provider를 호출하지 않습니다. 가사는 없으므로 가사 미완료 경고가 남는 것이 정상입니다. `builds/B0001/MASTER_CLEAN.mp4`에서 전곡 Preview를 확인하고 `build.json`에서 선택 자산과 경고를 확인합니다. 프로젝트가 이미 있으면 다른 `--name`을 사용합니다.

**이 결과는 연출과 타이밍을 보는 애니마틱입니다.** 도형 캐릭터와 배경은 공간·동선 검토용이며, 캐릭터 reference art와 완성 작화는 아닙니다. 로컬 홀드·팬·줌·레이어 이동이 걷기, 연기, 실제 활공 애니메이션을 대신하지 않습니다. `--quality final`은 출력 해상도 선택이며 Final 승인이나 LOCK을 뜻하지 않습니다. 기본 preset은 의도적인 `STATIC` 콘티 홀드로 시작하고, 그림·위치 레퍼런스는 모두 빈 배열과 검토 요청으로 남깁니다.

## 모션을 추가할 순서

먼저 옥상 출발 → 골목 하강 → 고가 통과 → 시장 전경 → 공터의 정적 → 따뜻한 귀갓길이 곡에 맞게 흐르는지 봅니다. 실곡에 적용할 때는 합성 음원의 구간을 그대로 확정하지 말고 실제 음악의 변화와 가사에 맞춰 컷을 수정합니다.

그다음 **8~20샷만** 모션 후보로 고릅니다. 높이와 깊이가 바뀌는 활공·하강, 기둥과 골목을 통과하는 추적/이동, 손을 내밀거나 시선을 돌리는 감정 피크가 우선입니다. 작은 손동작·옷자락·환경 움직임은 `LIMITED_MOTION`, 복잡한 공간 이동은 필요한 샷에만 `FULL_GENERATIVE`로 지정합니다. 캐릭터와 장소의 원본 레퍼런스 아트를 먼저 제작·검토하고, 선택 샷의 견적과 승인은 별도 단계로 진행합니다. 이번 preset과 예제에는 유료 생성, 전곡 모션 완성, Final LOCK이 포함되지 않습니다.

## 로컬 실행 확인 · 2026-09-15

위 240초 예제를 실제 실행해 `B0001` Preview를 만들었습니다. Python 3.12.14와 FFmpeg를 사용했고, 준비/컴파일 동안 소켓 연결과 유료 renderer 진입점을 차단했습니다.

| 항목 | 결과 |
| --- | --- |
| 음원 | 합성 테스트 신호, 분석된 길이 240,000ms |
| 타임라인 | 48샷, 6개 장소, Mira/Tov ID 일관 |
| 선택 자산 | 콘티 홀드 36 + 로컬 패럴랙스 초안 12 |
| 출력 | 1440×1080, 24fps, 5,760프레임, 4:00.000 |
| 무결성 | 원음 SHA 유지, 빌드 inventory 검증 통과, 전체 디코딩 통과 |
| 음성 | 모든 시각 clip 무음; 최종 mux에는 합성 master만 사용 |
| 유료 영상 생성 | 0회 |
| 검토 상태 | 가사 없음, 캐릭터/장소 레퍼런스 아트 미승인, Production/Final LOCK 없음 |

로컬 도형 원본은 960×720이며, 컴파일러가 1440×1080 출력으로 정규화합니다. 이는 화풍을 완성한 작화 검증이 아니라 도시 경로·팔레트·구도·카메라 타이밍 검증입니다. 신규 5개 테스트는 preset 로딩, 네 Bible 출력, 데이터 독립성 및 네트워크/유료 renderer를 차단한 짧은 Preview를 검사합니다. 기존 98개 회귀시험도 통과했습니다. 바이너리 음원·영상은 Git에 넣지 않고 예제 명령으로 재생성합니다.
