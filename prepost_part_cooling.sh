#!/bin/bash
# One node's share of a Miyabi prepost 3-D run for the cooling remeasurement (stereo_acoustools_3d_records_V15c).
# Called by prepost_main_cooling.sh on the first node and through pbsdsh on the second. The postprocess options
# are fixed here, not passed through the environment: qsub -I does not carry the caller's environment into the
# job, so an EXTRA_ARGS exported before qsub never reached run_prepost_post_list.sh (2026-10-10).
#   $1 part list (run dirs, one per line)  $2 calibration npz  $3 camera->PAT npz (fixed comparison)
cd /work/xg25g006/x10733/eventcam/stereo_3d || exit 1
PART=$1; CALIB=$2; TRANSFORM=$3; NPAR=${NPAR:-20}
export PYTHONPATH="${HOME}/.local/lib/python3.12/site-packages:${PYTHONPATH:-}"
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 MPLBACKEND=Agg STEREO_TRACK_PARALLEL=1
EXTRA="--left-mask-roi-override ${LEFT_MASK:-1160,0,1280,720} --reacquire-after-sec 0.005 --reacquire-bins 3"
echo "--- $(hostname) $(grep -c . "$PART") runs $(date) npar=$NPAR calib=$CALIB transform=$TRANSFORM extra=$EXTRA"
# shellcheck disable=SC2046
python3 patch_manifest_paths_for_miyabi.py --calibration "$CALIB" $(cat "$PART")
# shellcheck disable=SC2086
xargs -P "$NPAR" -I{} python3 stereo_acoustools_3d_postprocess.py {} --camera-to-pat-transform "$TRANSFORM" \
  --auto-time-search-sec 0 $EXTRA < "$PART"
while read -r d; do
  [ -f "$d/stereo_recording/stereo_3d/stereo_3d_points.npz" ] && echo "3D OK: $(basename "$d")" || echo "NO 3D: $(basename "$d")"
done < "$PART"
echo "[PART DONE] $(hostname) $(date)"
