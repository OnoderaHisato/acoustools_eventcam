#!/bin/bash
# ==============================================================================
# run_full_sindy_miyabi.sh  --  Full SINDy (stereo3D) 用 Miyabi バッチ管理
#
# sindy_stereo3d_full_tune.py の prepare/run-task/aggregate/evaluate を
# run_0328_miyabi.sh と同じ操作感 (push→投入→status→fetch) でラップする。
#
# 【使い方】
#   ./run_full_sindy_miyabi.sh              # コード+データ転送 & prepare & 配列ジョブ投入
#   ./run_full_sindy_miyabi.sh --smoke       # 投入前にタスク0-3だけで動作確認 (推奨、初回は必須)
#   ./run_full_sindy_miyabi.sh --status      # 配列ジョブの状態確認 (qstat)
#   ./run_full_sindy_miyabi.sh --log         # 最新ログ末尾を表示
#   ./run_full_sindy_miyabi.sh --evaluate    # 全タスク完了後: aggregate + evaluate を投入
#   ./run_full_sindy_miyabi.sh --fetch       # 結果 (summary系) をローカルへ取得
#   ./run_full_sindy_miyabi.sh --fetch-all   # tasks/*.json も含めて全部取得 (重い)
#   ./run_full_sindy_miyabi.sh --kill        # 配列ジョブをキャンセル
#
# RUN_NAME 環境変数で run-dir 名を指定可能 (省略時は投入時刻から自動生成、
# --status 以降は .full_sindy_run_name に保存された値を再利用する)。
# ==============================================================================

set -euo pipefail

SSH_HOST="miyabi-c.jcahpc.jp"
# データ (stereo_acoustools_3d_records_auto*) はユーザーが SFTP で
# /work/xg25g006/x10733/eventcam/stereo_3d/ 直下に既に転送済み (2026-08-20)。
# コードもここに置き、--base-dir は "." (このディレクトリ自身) を指す。
REMOTE_DIR="/work/xg25g006/x10733/eventcam/stereo_3d"
RUN_NAME_FILE=".full_sindy_run_name"
JOB_ID_FILE=".full_sindy_job_id"
MODE="${1:-}"

CODE_FILES=(
    sindy_stereo3d_data.py
    sindy_stereo3d_full.py
    sindy_stereo3d_full_tune.py
    full_sindy_search_config.json
    run_full_sindy_miyabi.pbs
    run_full_sindy_miyabi_chunk.pbs
    evaluate_full_sindy_miyabi.pbs
    requirements_miyabi.txt
)

# Miyabi-C の xg25g006 グループ制限 (ACCEPT<=4, RUN<=2) は PBS 配列ジョブの
# サブジョブ1本1本を個別にACCEPT枠として消費するため、208要素の配列ジョブは
# 一括投入できない (qsub: would exceed group xg25g006's limit on resource
# njobs-c in complex; 実測: 配列4要素までは投入可、10要素は拒否)。
# そのため配列ジョブではなく、通常ジョブ4本 (それぞれ内部でタスク番号を
# 逐次ループ) に分割する。ACCEPT=4ちょうど、RUN=2により実質2並列は維持される。
NUM_CHUNKS=4

push_code_and_data() {
    echo "=== コード転送 ==="
    ssh "${SSH_HOST}" "mkdir -p ${REMOTE_DIR}"
    scp "${CODE_FILES[@]}" "${SSH_HOST}:${REMOTE_DIR}/"
    echo "=== データセットはユーザーが SFTP で ${REMOTE_DIR}/ に転送済みのためスキップ ==="
    echo "    (stereo_acoustools_3d_records_auto{,_additional,_retention_boundary,_scale80,_scale85,_scale90,_scale95})"
    ssh "${SSH_HOST}" "find ${REMOTE_DIR} -maxdepth 1 -name 'stereo_acoustools_3d_records_auto*' -exec sh -c 'echo -n {}\": \"; find {} -name state_control.npz | wc -l' \;"
}

# ==============================================================================
# --smoke: 本投入前にタスク 0-3 だけ (%2 で最大2並列) をローカル環境で流し、
#          リモートに1件も投入せず所要時間の桁を確認する。
#          過去に threshold が効かない・ZOHで発散する等の踏み抜きがあったため、
#          1664本を流す前に必ずここで健全性を確認する。
# ==============================================================================
if [[ "$MODE" == "--smoke" ]]; then
    echo "=== ローカルでタスク 0 / 1 / 2 の所要時間を確認 (anchor1=軽量 / anchor2=重量 / anchor3=中量) ==="
    TMP_RUN=$(mktemp -d)/full_sindy_smoke
    python3 sindy_stereo3d_full_tune.py prepare \
        --config full_sindy_search_config.json \
        --base-dir Acoutools_eventcam \
        --run-dir "${TMP_RUN}"
    for idx in 0 1 2; do
        echo "--- task ${idx} ---"
        time python3 sindy_stereo3d_full_tune.py run-task --run-dir "${TMP_RUN}" --task-index "${idx}" || true
    done
    echo "所要時間を確認したうえで、必要なら full_sindy_search_config.json の探索空間"
    echo "(weak_stride_ms / weak_half_width_ms / bootstrap_repeats / downsample) を絞ってから投入してください。"
    exit 0
fi

# ==============================================================================
# --status
# ==============================================================================
if [[ "$MODE" == "--status" ]]; then
    if [[ ! -f "$JOB_ID_FILE" ]]; then
        echo "ジョブIDファイルが見つかりません（未投入またはクリア済み）"
        exit 0
    fi
    RUN_NAME=$(cat "$RUN_NAME_FILE" 2>/dev/null || echo "?")
    echo "Run: ${RUN_NAME}"
    echo "--- ジョブ状態 (4分割チャンクジョブ; ACCEPT<=4/RUN<=2制限のため通常ジョブ4本構成) ---"
    while read -r JOB_ID; do
        [[ -z "$JOB_ID" ]] && continue
        echo "[${JOB_ID}]"
        ssh -n "${SSH_HOST}" "qstat -f ${JOB_ID} 2>/dev/null | grep -E 'job_state|resources_used.walltime|comment' | sed 's/^/  /' || echo '  終了済みまたは存在しない'"
    done < "$JOB_ID_FILE"
    echo ""
    echo "--- Miyabi-C グループ (xg25g006) リソース状況 ---"
    ssh "${SSH_HOST}" "qstat --limit 2>/dev/null | sed -n '/Miyabi-C/,+2p'"
    echo ""
    echo "--- タスク完了数 ---"
    ssh "${SSH_HOST}" "ls ${REMOTE_DIR}/${RUN_NAME}/tasks/*.json 2>/dev/null | wc -l"
    echo "（総タスク数は task_count.txt を参照）"
    ssh "${SSH_HOST}" "cat ${REMOTE_DIR}/${RUN_NAME}/task_count.txt 2>/dev/null"
    exit 0
fi

# ==============================================================================
# --log
# ==============================================================================
if [[ "$MODE" == "--log" ]]; then
    ssh "${SSH_HOST}" "ls -t ${REMOTE_DIR}/full_sindy.o* 2>/dev/null | head -1 | xargs tail -40 2>/dev/null || echo '（ログなし）'"
    exit 0
fi

# ==============================================================================
# --evaluate: 配列ジョブが完了したあとに aggregate + evaluate を投入
# ==============================================================================
if [[ "$MODE" == "--evaluate" ]]; then
    RUN_NAME=$(cat "$RUN_NAME_FILE")
    echo "=== evaluate 投入: ${RUN_NAME} ==="
    ssh "${SSH_HOST}" "cd ${REMOTE_DIR} && qsub -v RUN_DIR=${RUN_NAME} evaluate_full_sindy_miyabi.pbs"
    exit 0
fi

# ==============================================================================
# --fetch / --fetch-all
# ==============================================================================
if [[ "$MODE" == "--fetch" || "$MODE" == "--fetch-all" ]]; then
    RUN_NAME=$(cat "$RUN_NAME_FILE")
    echo "=== 結果を取得中: ${RUN_NAME} ==="
    mkdir -p "sindy_stereo3d_output/${RUN_NAME}"
    scp "${SSH_HOST}:${REMOTE_DIR}/${RUN_NAME}"/{manifest.json,selected_settings.json,inner_cv_ranking.csv,outer_cv_metrics.csv,outer_cv_models.json,outer_cv_summary.json,outer_cv_rollout.png,final_model.json,final_equations.txt} \
        "sindy_stereo3d_output/${RUN_NAME}/" 2>/dev/null || true
    if [[ "$MODE" == "--fetch-all" ]]; then
        echo "=== tasks/*.json も取得 (時間がかかります) ==="
        rsync -av "${SSH_HOST}:${REMOTE_DIR}/${RUN_NAME}/tasks/" "sindy_stereo3d_output/${RUN_NAME}/tasks/"
    fi
    echo "取得完了: sindy_stereo3d_output/${RUN_NAME}/"
    exit 0
fi

# ==============================================================================
# --kill
# ==============================================================================
if [[ "$MODE" == "--kill" ]]; then
    if [[ ! -f "$JOB_ID_FILE" ]]; then
        echo "ジョブIDファイルが見つかりません"
        exit 1
    fi
    while read -r JOB_ID; do
        [[ -z "$JOB_ID" ]] && continue
        ssh -n "${SSH_HOST}" "qdel ${JOB_ID} && echo 'キャンセルしました: ${JOB_ID}'" || true
    done < "$JOB_ID_FILE"
    rm -f "$JOB_ID_FILE"
    exit 0
fi

# ==============================================================================
# デフォルト: push -> prepare -> 配列ジョブ投入
# ==============================================================================
RUN_NAME="${RUN_NAME:-full_sindy_miyabi_$(date +%Y%m%d_%H%M%S)}"

push_code_and_data

echo "=== prepare (リモート) ==="
ssh "${SSH_HOST}" "cd ${REMOTE_DIR} && \
    export PYTHONPATH=\${HOME}/.local/lib/python3.12/site-packages:\${PYTHONPATH:-} && \
    python3 sindy_stereo3d_full_tune.py prepare \
        --config full_sindy_search_config.json \
        --base-dir . \
        --run-dir ${RUN_NAME}"

TASK_COUNT=$(ssh "${SSH_HOST}" "cat ${REMOTE_DIR}/${RUN_NAME}/task_count.txt")
echo "総タスク数: ${TASK_COUNT}"
if [[ "$TASK_COUNT" -ne 208 ]]; then
    echo "警告: 想定タスク数 208 と一致しません (実際: ${TASK_COUNT})。"
    echo "      NUM_CHUNKS=${NUM_CHUNKS} での分割範囲を見直してから手動投入してください。"
    exit 1
fi

echo "=== チャンクジョブ ${NUM_CHUNKS} 本投入 (Miyabi-C の xg25g006 グループ制限 ACCEPT<=4/RUN<=2 に" \
     "合わせ、配列ジョブではなく通常ジョブ${NUM_CHUNKS}本に分割。各ジョブ内部でタスク番号を逐次実行) ==="
rm -f "$JOB_ID_FILE"
CHUNK_SIZE=$(( (TASK_COUNT + NUM_CHUNKS - 1) / NUM_CHUNKS ))
for ((c = 0; c < NUM_CHUNKS; c++)); do
    START_IDX=$((c * CHUNK_SIZE))
    END_IDX=$(((c + 1) * CHUNK_SIZE - 1))
    if [[ "$END_IDX" -ge "$TASK_COUNT" ]]; then
        END_IDX=$((TASK_COUNT - 1))
    fi
    if [[ "$START_IDX" -gt "$END_IDX" ]]; then
        continue
    fi
    echo "  chunk ${c}: tasks ${START_IDX}-${END_IDX}"
    JOB_ID=$(ssh "${SSH_HOST}" "cd ${REMOTE_DIR} && qsub -v RUN_DIR=${RUN_NAME},START_IDX=${START_IDX},END_IDX=${END_IDX} run_full_sindy_miyabi_chunk.pbs")
    echo "$JOB_ID" >> "$JOB_ID_FILE"
    echo "  -> Job ID: ${JOB_ID}"
done
echo "$RUN_NAME" > "$RUN_NAME_FILE"
echo "Run dir: ${REMOTE_DIR}/${RUN_NAME}"
echo ""
echo "208タスク(stlsq x full, 8条件)を${NUM_CHUNKS}本のジョブ(RUN<=2で実質2並列)で流すため"
echo "完走まで2日程度の見込みです。--status で定期確認してください。"
