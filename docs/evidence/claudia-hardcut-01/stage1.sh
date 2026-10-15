#!/bin/bash
# Stage 1 of the real run: master -> project -> analyze -> supplied demucs stem -> lyrics-align
set -e
WT=/workspace/film-run/wt-claudia; PY=/tmp/claudia-venv/bin/python; P=$WT/projects/arirang_hardcut
cd $WT
date -Is; echo "monotonic $(cut -d' ' -f1 /proc/uptime)"
ffmpeg -v error -y -i /tmp/claudia-song/arirang_yoon_2021.flac -c:a pcm_s16le /tmp/claudia-run/master.wav
sha256sum /tmp/claudia-song/arirang_yoon_2021.flac /tmp/claudia-run/master.wav
cat > /tmp/claudia-run/brief.md <<'B'
2021 아리랑 응원가(윤도현 가창, CC BY 4.0 — 한국저작권위원회 공유마당 / Wikimedia Commons). 2D 셀 애니메이션 MV의 한 장면 시험.
주인공: 짧은 단발의 젊은 여성, 청록색 재킷. 아리랑 고개를 바라보며 노래한다. 실사 표현 금지.
B
$PY -m engine.cli init arirang_hardcut --audio /tmp/claudia-run/master.wav --brief /tmp/claudia-run/brief.md --lyrics /tmp/claudia-song/lyrics.txt --aspect 16:9 2>&1 | tail -2 || $PY -m engine.cli init arirang_hardcut --audio /tmp/claudia-run/master.wav --brief /tmp/claudia-run/brief.md --lyrics /tmp/claudia-song/lyrics.txt 2>&1 | tail -2
ls $P
$PY -m engine.cli analyze $P > /tmp/claudia-run/analyze.json 2>&1; tail -c 300 /tmp/claudia-run/analyze.json; echo
cp /tmp/claudia-song/stems/htdemucs/arirang_yoon_2021/vocals.wav $P/input/vocals.wav
$PY -m engine.cli lyrics-prepare $P > /dev/null
T0=$(cut -d' ' -f1 /proc/uptime)
$PY -m engine.cli lyrics-align $P --model small --compute-type float32 > /tmp/claudia-run/align.json 2> /tmp/claudia-run/align.err
T1=$(cut -d' ' -f1 /proc/uptime)
echo "align wall (monotonic) s: $(python3 -c "print(round($T1-$T0,1))")"
cat /tmp/claudia-run/align.json
echo STAGE1_DONE
