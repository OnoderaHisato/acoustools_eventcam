#!/bin/bash
# Re-process runs whose left 2D track was cut by the PAT-start LED mask (2026-09-26, user-approved).
# Run INSIDE the onodera_sindy container (dngstation / deepstation rules), detached, e.g.
#   docker exec -d onodera_sindy bash -c 'cd /root/share/eventcam/stereo_3d && \
#     (NPAR=6 bash run_leftmask_reprocess_docker.sh list_X.txt > logs_reprocess_20260926/X.log 2>&1 & \
#      echo $! > logs_reprocess_20260926/X.pid)'
# Per run: postprocess with the patched scripts in _leftmask_reacquire_20260926/
#   (tracking-only left mask LEFT_MASK instead of pat_start_led.roi, re-acquisition after 5 ms;
#    the LED ROI and the recorded PAT-start time are not touched)
# -> fixed camera->PAT comparison against u (and r when a reference log exists), exactly as
#    run_ff_heart_compare_list.sh does, but per run and with logs under logs_reprocess_20260926/.
set -u
LIST=${1:?usage: run_leftmask_reprocess_docker.sh <run dir list file>}
cd "${STEREO3D_DIR:-$(cd "$(dirname "$0")" && pwd)}" || exit 1
NPAR=${NPAR:-6}
if [ "${NPAR}" -gt 6 ]; then NPAR=6; fi
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 MPLBACKEND=Agg
export OPENCV_FOR_THREADS_NUM=${OPENCV_THREADS:-2}
export STEREO_TRACK_PARALLEL=1
export LEFT_MASK=${LEFT_MASK:-748,0,892,52}
export REACQUIRE_AFTER_SEC=${REACQUIRE_AFTER_SEC:-0.005}
export REACQUIRE_BINS=${REACQUIRE_BINS:-3}
export SCRIPTS=_leftmask_reacquire_20260926
export TRANSFORM_POST=camera_to_pat_pooled_2s_20260916.npz
export TRANSFORM_FIXED=${TRANSFORM_FIXED:-camera_to_pat_pooled_ffheart_20260918.npz}
export LOGDIR=logs_reprocess_20260926
mkdir -p "${LOGDIR}"

one_run() {
  local R=$1 name log cmp npz offset ideal ref
  name=$(basename "${R}")
  log=${LOGDIR}/${name}.post.log
  cmp=${LOGDIR}/${name}.compare.log
  echo "[RUN START] ${name} $(date +%T)"
  if ! python3 "${SCRIPTS}/stereo_acoustools_3d_postprocess.py" "${R}" \
      --camera-to-pat-transform "${TRANSFORM_POST}" --auto-time-search-sec 0 \
      --left-mask-roi-override "${LEFT_MASK}" --reacquire-after-sec "${REACQUIRE_AFTER_SEC}" \
      --reacquire-bins "${REACQUIRE_BINS}" > "${log}" 2>&1; then
    echo "[RUN FAIL] ${name} postprocess (see ${log})"; tail -3 "${log}"; return 0
  fi
  npz=${R}/stereo_recording/stereo_3d/stereo_3d_points.npz
  offset=$(python3 ffheart_time_origin.py "${R}/pat_camera_timing.json")
  ideal=$(ls "${R}"/*_ideal_log.csv | head -1)
  if python3 stereo_compare_ideal_3d.py "${npz}" "${ideal}" --output-dir "${R}/ideal_comparison_3d" \
      --spatial-alignment fixed --camera-to-pat-transform "${TRANSFORM_FIXED}" --time-offset-sec "${offset}" \
      --auto-time-search-sec 0 > "${cmp}" 2>&1; then
    echo "[OK] u  ${name}  $(grep RMSE "${cmp}" | head -1)"
  else
    echo "[RUN FAIL] ${name} compare u"; tail -2 "${cmp}"; return 0
  fi
  ref=$(ls "${R}"/*_reference_log.csv 2>/dev/null | head -1)
  if [ -n "${ref}" ]; then
    if python3 stereo_compare_ideal_3d.py "${npz}" "${ref}" --output-dir "${R}/reference_comparison_3d" \
        --spatial-alignment fixed --camera-to-pat-transform "${TRANSFORM_FIXED}" --time-offset-sec "${offset}" \
        --auto-time-search-sec 0 > "${cmp}.r" 2>&1; then
      echo "[OK] r  ${name}  $(grep RMSE "${cmp}.r" | head -1)"
    else
      echo "[RUN FAIL] ${name} compare r"; tail -2 "${cmp}.r"; return 0
    fi
  fi
  echo "[RUN DONE] ${name} $(date +%T)"
}
export -f one_run

echo "=== host=$(hostname) npar=${NPAR} mask=${LEFT_MASK} reacquire=${REACQUIRE_AFTER_SEC}s/${REACQUIRE_BINS} start=$(date) list=${LIST} runs=$(wc -l < "${LIST}") ==="
xargs -P "${NPAR}" -I{} bash -c 'one_run "$1"' _ {} < "${LIST}"
echo "[DONE] $(date)"
