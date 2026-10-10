#!/bin/bash
# Miyabi prepost (prepost1_n2, 2 nodes) job body for the cooling remeasurement. Submit from a miyabi-c tmux
# after the slot has started, e.g.
#   tmux new -d -s post_X "qsub -I -q prepost1_n2 -l select=2 -l walltime=05:50:00 -W group_list=xg25g006 -- \
#     /bin/bash /work/xg25g006/x10733/eventcam/stereo_3d/prepost_main_cooling.sh list_X.txt \
#     stereo_calibration_square7p12_final_41poses.npz camera_to_pat_pooled_cooled_20261010.npz"
# Splits the list in halves: the first runs here, the second on the other node through pbsdsh. Both halves run
# prepost_part_cooling.sh (a script file, so nothing has to be quoted through pbsdsh).
cd /work/xg25g006/x10733/eventcam/stereo_3d || exit 1
LIST=$1; CALIB=$2; TRANSFORM=$3
N=$(grep -c . "$LIST"); H=$(( (N + 1) / 2 ))
head -n "$H" "$LIST" > "$LIST.part1"; tail -n +$((H + 1)) "$LIST" > "$LIST.part2"
NODES=$(sort -u "${PBS_NODEFILE:-/dev/null}" 2>/dev/null | wc -l)
LOG=results_prepost_$(basename "$LIST" .txt)
echo "=== prepost: host=$(hostname) nodes=$NODES start=$(date) list=$LIST runs=$N ==="
if [ "$NODES" -ge 2 ] && [ -s "$LIST.part2" ]; then
  /opt/pbs/bin/pbsdsh -n 1 -- /bin/bash "$PWD/prepost_part_cooling.sh" "$PWD/$LIST.part2" "$CALIB" "$TRANSFORM" > "$LOG.node2" 2>&1 &
  bash prepost_part_cooling.sh "$LIST.part1" "$CALIB" "$TRANSFORM" 2>&1 | tee "$LOG.node1"
  wait
else
  bash prepost_part_cooling.sh "$LIST" "$CALIB" "$TRANSFORM" 2>&1 | tee "$LOG.node1"
fi
echo "[DONE] $(date)"
