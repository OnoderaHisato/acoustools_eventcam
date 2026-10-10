#!/bin/bash
# 3-D processing of the cooling remeasurement runs (stereo_acoustools_3d_records_V15c, 2026-10-10 on).
# Run INSIDE the user's container on dngstation (onodera_pinn), detached, e.g.
#   docker exec -d onodera_pinn bash -c 'cd /root/share/eventcam/stereo_3d && \
#     (NPAR=6 bash run_cooling_post_docker.sh list_X.txt > logs_cooling/X.log 2>&1 & echo $! > logs_cooling/X.pid)'
# Differences from run_post_and_compare_list_docker.sh / run_leftmask_reprocess_docker.sh:
#   - the stereo calibration is the 2026-10-09 one (stereo_calibration_square7p12_final_41poses.npz);
#     every manifest must already name that file, otherwise the run is refused;
#   - the left tracking mask covers the PAT-start LED: 1160,0,1280,240 (the LED sat at x 1182-1278, y 0-99 on
#     10-09 and at x 1191-1254, y 127-204 on 10-10; the LED ROI for timing is untouched);
#   - the postprocess comparison is the per-run rigid fit (shape only); after it, when TRANSFORM_FIXED exists
#     (default camera_to_pat_pooled_cooled_20261010.npz, made by the analysis side from block 0 on 2026-10-10),
#     the fixed comparison against u (ideal_comparison_3d) and, when a reference log exists, r
#     (reference_comparison_3d) is run, as run_leftmask_reprocess_docker.sh does. TRANSFORM_FIXED= skips it.
set -u
LIST=${1:?usage: run_cooling_post_docker.sh <run dir list file>}
cd "${STEREO3D_DIR:-$(cd "$(dirname "$0")" && pwd)}" || exit 1
NPAR=${NPAR:-6}
if [ "${NPAR}" -gt 6 ]; then NPAR=6; fi
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 MPLBACKEND=Agg
export OPENCV_FOR_THREADS_NUM=${OPENCV_THREADS:-2}
export STEREO_TRACK_PARALLEL=1
export CALIBRATION=${CALIBRATION:-stereo_calibration_square7p12_final_41poses.npz}
export LEFT_MASK=${LEFT_MASK:-1160,0,1280,240}
export REACQUIRE_AFTER_SEC=${REACQUIRE_AFTER_SEC:-0.005}
export REACQUIRE_BINS=${REACQUIRE_BINS:-3}
export SCRIPTS=${SCRIPTS:-_leftmask_reacquire_20260926}
export LOGDIR=${LOGDIR:-logs_cooling}
export TRANSFORM_FIXED=${TRANSFORM_FIXED-camera_to_pat_pooled_cooled_20261010.npz}
mkdir -p "${LOGDIR}"
RUN_DIRS=$(cat "${LIST}")
echo "=== host=$(hostname) npar=${NPAR} calibration=${CALIBRATION} mask=${LEFT_MASK} start=$(date) list=${LIST} runs=$(echo "${RUN_DIRS}" | wc -l) ==="
python3 --version
[ -f "${CALIBRATION}" ] || { echo "calibration ${CALIBRATION} not found"; exit 1; }
# shellcheck disable=SC2086
python3 patch_manifest_paths_for_miyabi.py --calibration "${CALIBRATION}" ${RUN_DIRS} || exit 1

one_run() {
  local R=$1 name log
  name=$(basename "${R}")
  log=${LOGDIR}/${name}.post.log
  if ! python3 -c "import json,os,sys; m=json.load(open(sys.argv[1]+'/pipeline_manifest.json')); sys.exit(os.path.basename(m['stereo_calibration'])!=sys.argv[2])" "${R}" "${CALIBRATION}"; then
    echo "[REFUSED] ${name}: manifest names another calibration"; return 0
  fi
  echo "[RUN START] ${name} $(date +%T)"
  python3 "${SCRIPTS}/stereo_acoustools_3d_postprocess.py" "${R}" --auto-time-search-sec 0 \
    --left-mask-roi-override "${LEFT_MASK}" --reacquire-after-sec "${REACQUIRE_AFTER_SEC}" \
    --reacquire-bins "${REACQUIRE_BINS}" > "${log}" 2>&1
  local rc=$?
  local npz=${R}/stereo_recording/stereo_3d/stereo_3d_points.npz
  if [ -f "${npz}" ]; then
    echo "[3D OK] ${name} exit=${rc} $(date +%T) $(grep -m1 'RMSE' "${log}")"
  else
    echo "[NO 3D] ${name} exit=${rc} (see ${log})"; tail -3 "${log}"; return 0
  fi
  if [ -n "${TRANSFORM_FIXED}" ] && [ -f "${TRANSFORM_FIXED}" ]; then
    local offset ideal ref cmp=${LOGDIR}/${name}.compare.log
    offset=$(python3 ffheart_time_origin.py "${R}/pat_camera_timing.json")
    ideal=$(ls "${R}"/*_ideal_log.csv | head -1)
    if python3 stereo_compare_ideal_3d.py "${npz}" "${ideal}" --output-dir "${R}/ideal_comparison_3d"         --spatial-alignment fixed --camera-to-pat-transform "${TRANSFORM_FIXED}" --time-offset-sec "${offset}"         --auto-time-search-sec 0 > "${cmp}" 2>&1; then
      echo "[FIXED u] ${name} $(grep -m1 RMSE "${cmp}")"
    else
      echo "[FIXED FAIL] ${name} u (see ${cmp})"; tail -2 "${cmp}"
    fi
    ref=$(ls "${R}"/*_reference_log.csv 2>/dev/null | head -1)
    if [ -n "${ref}" ]; then
      if python3 stereo_compare_ideal_3d.py "${npz}" "${ref}" --output-dir "${R}/reference_comparison_3d"           --spatial-alignment fixed --camera-to-pat-transform "${TRANSFORM_FIXED}" --time-offset-sec "${offset}"           --auto-time-search-sec 0 > "${cmp}.r" 2>&1; then
        echo "[FIXED r] ${name} $(grep -m1 RMSE "${cmp}.r")"
      else
        echo "[FIXED FAIL] ${name} r (see ${cmp}.r)"; tail -2 "${cmp}.r"
      fi
    fi
  fi
}
export -f one_run
echo "${RUN_DIRS}" | xargs -P "${NPAR}" -I{} bash -c 'one_run "$1"' _ {}
echo "[DONE] $(date)"
