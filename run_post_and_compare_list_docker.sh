#!/bin/bash
# dngstation (sindy-server2) version of run_post_and_compare_list.pbs: run it INSIDE the onodera_sindy
# container, never on the host (DNGSTATION_RULES.md). Same steps as the Miyabi job: fix the Windows
# paths in the manifests -> 2D tracking + 3D (NPAR runs at a time) -> compare with the fixed
# camera->PAT transform.
#
# Launch detached from the host so it survives the ssh session ending (rule 6):
#   docker exec -d onodera_sindy bash -c 'cd /root/share/eventcam/stereo_3d && \
#     (NPAR=6 bash run_post_and_compare_list_docker.sh list_X.txt > logs_dngstation/X.log 2>&1 & \
#      echo $! > logs_dngstation/X.pid)'
# NPAR is capped at 6: about 3.75 cores per run on a shared 40-core machine.
set -u
LIST=${1:?usage: run_post_and_compare_list_docker.sh <run dir list file>}
cd "${STEREO3D_DIR:-$(cd "$(dirname "$0")" && pwd)}" || exit 1
NPAR=${NPAR:-6}
if [ "${NPAR}" -gt 6 ]; then NPAR=6; fi
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 MPLBACKEND=Agg
# OpenCV in this container starts one thread per host core (40) and ignores OMP_NUM_THREADS; one run then
# took about 13 cores. Two threads per camera tracker keep a run at about 4 cores (left + right in parallel).
export OPENCV_FOR_THREADS_NUM=${OPENCV_THREADS:-2}
export STEREO_TRACK_PARALLEL=1      # track the left and right cameras at the same time
CALIBRATION="stereo_calibration_square7p12_extrinsics_final.npz"
TRANSFORM="camera_to_pat_pooled_2s_20260916.npz"
RUN_DIRS=$(cat "${LIST}")
echo "=== host=$(hostname) ncpus=$(nproc --all) npar=${NPAR} opencv_threads=${OPENCV_FOR_THREADS_NUM} start=$(date) list=${LIST} runs=$(echo "${RUN_DIRS}" | wc -l) ==="
python3 --version
# shellcheck disable=SC2086
python3 patch_manifest_paths_for_miyabi.py --calibration "${CALIBRATION}" ${RUN_DIRS}
echo "${RUN_DIRS}" | xargs -P "${NPAR}" -I{} python3 stereo_acoustools_3d_postprocess.py {} --camera-to-pat-transform "${TRANSFORM}" --auto-time-search-sec 0
for d in ${RUN_DIRS}; do [ -f "$d/stereo_recording/stereo_3d/stereo_3d_points.npz" ] && echo "3D OK: $(basename "$d")" || echo "NO 3D: $(basename "$d")"; done
echo "=== compare (fixed camera->PAT transform) ==="
TRANSFORM=${TRANSFORM_FIXED:-camera_to_pat_pooled_ffheart_20260918.npz} bash run_ff_heart_compare_list.sh "${LIST}"
echo "[DONE] $(date)"
