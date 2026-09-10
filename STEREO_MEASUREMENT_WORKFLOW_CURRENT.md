# Current Stereo Measurement Workflow

> **履歴資料です。ここに残る7.1 mmおよびblink5のコマンドは再実行しないでください。**
> 2026-07-27以降の現行手順は
> [CAMERA_PAT_RECALIBRATION_WORKFLOW_JP.md](CAMERA_PAT_RECALIBRATION_WORKFLOW_JP.md)
> です。現行の正方形一辺は **7.12 mm** です。

## 1. Purpose and current status

This document records the reproducible path from event-camera checkerboard
images to a 3D particle trajectory.  It also separates completed work from
the remaining camera-to-PAT registration.

Completed:

1. Intrinsic calibration for both cameras.
2. Stereo extrinsic calibration between the cameras.
3. Paired event recording, 2D particle tracking, and 3D triangulation.
4. Camera-frame 3D plotting, overlay video generation, and the interactive
   viewer with the camera-frame CAD overlay.

Not yet completed:

1. A measured rigid transform from the left-camera coordinate system to the
   PAT coordinate system.
2. PAT-coordinate trajectories and quantitative comparison with an AcousTools
   ideal log.

All length values below are in mm unless noted otherwise.

## 2. Names, camera assignment, and coordinate frames

### Final camera assignment

| Role | Serial | Intrinsic calibration directory |
| --- | --- | --- |
| Left / reference camera | `00000508` | `checkerboard_calib_blink5_cam2` |
| Right camera | `00000509` | `checkerboard_calib_blink5_cam1` |

The directory names `cam1` and `cam2` are historical capture names.  Use the
serial numbers and the roles above for all later commands.

### Checkerboard definition

- 10 x 7 squares
- 9 x 6 internal corners
- square side: `7.1 mm`

### Coordinate frames

1. Image coordinates: `u` is right and `v` is down, in pixels.
2. Camera-frame 3D output: left OpenCV camera coordinates.  The left optical
   centre is `[0, 0, 0]`; `X` is right, `Y` is down, and `Z` is forward.
3. Viewer display: the interactive viewer mirrors the camera frame as
   `(-X, -Y, +Z)` so the visual orientation is easier to inspect.  It does not
   modify the NPZ or CSV data.
4. PAT frame: not yet measured.  It will be produced by the 3D static-grid
   registration described in section 8.

## 3. Step A: single-camera intrinsic calibration (completed)

### Input

- Checkerboard event images for one camera, rendered as PNG images.
- Known `7.1 mm` square side.

The capture script records a short RAW file per pose, exports an event NPZ,
and writes one selected candidate image to `calib_images/`.

### Capture command for a future re-capture

Run separately for each serial.  The following uses the settings that were
successful for the final stereo checkerboard set.

```powershell
.\venv\Scripts\python.exe eventcam_checkerboard_calibration_capture.py `
  --serial 00000508 `
  --output-dir checkerboard_calib_blink5_cam2 `
  --duration-sec 0.6 `
  --frame-window-us 20000 `
  --render-mode off `
  --candidate-count 1
```

```powershell
.\venv\Scripts\python.exe eventcam_checkerboard_calibration_capture.py `
  --serial 00000509 `
  --output-dir checkerboard_calib_blink5_cam1 `
  --duration-sec 0.6 `
  --frame-window-us 20000 `
  --render-mode off `
  --candidate-count 1
```

Capture varied board positions: centre and edges of the field of view, with
roll, pitch, and yaw.  The entire board must be visible.  The script continues
the pose numbering instead of overwriting existing files.

### Calibration commands used to make the selected intrinsic files

```powershell
.\venv\Scripts\python.exe single_camera_calibrate.py `
  --images checkerboard_calib_blink5_cam2\calib_images `
  --square-mm 7.1 `
  --output checkerboard_calib_blink5_cam2\silky_single_calibration_final_square7p1.npz `
  --debug-dir checkerboard_calib_blink5_cam2\corner_debug_final
```

```powershell
.\venv\Scripts\python.exe single_camera_calibrate.py `
  --images checkerboard_calib_blink5_cam1\calib_images `
  --square-mm 7.1 `
  --output checkerboard_calib_blink5_cam1\silky_single_calibration_final_square7p1.npz `
  --debug-dir checkerboard_calib_blink5_cam1\corner_debug_final
```

### Outputs and evaluation

| Output | Meaning | How it is used next |
| --- | --- | --- |
| `silky_single_calibration_final_square7p1.npz` | Camera matrix, distortion, 9 x 6 board definition, RMS, and per-view errors | Input to stereo calibration |
| Same-name CSV | Per-view reprojection errors | Inspect outlying poses |
| `corner_debug_final/` | Corner-detection overlays | Confirm that every accepted board has the correct corners |

Selected results:

| Camera | RMS reprojection error |
| --- | --- |
| Left `00000508` / cam2 | `0.184228 px` |
| Right `00000509` / cam1 | `0.192760 px` |

RMS is the image-space distance between an observed checkerboard corner and
the corresponding corner predicted by the fitted camera model.  It is not yet
a 3D particle-position error in mm.

## 4. Step B: stereo extrinsic calibration (completed)

### Input

- Both final intrinsic NPZ files from step A.
- Paired left/right checkerboard images at the same physical board poses.

### Capture command for a future re-capture

The two cameras are captured sequentially because simultaneous access can be
unstable on this PC.  The snapshot dual preview is only for positioning; it is
not the calibration data.

```powershell
.\venv\Scripts\python.exe stereo_checkerboard_calibration_capture.py `
  --left-serial 00000508 `
  --right-serial 00000509 `
  --output-dir stereo_checkerboard_blink5 `
  --dual-preview `
  --dual-preview-mode snapshot `
  --snapshot-sec 0.02 `
  --snapshot-max-events 50000 `
  --camera-reopen-wait-sec 1.0 `
  --duration-sec 0.6 `
  --capture-backend raw-log `
  --frame-window-us 20000 `
  --render-mode off
```

The completed capture has 45 poses.  For the final fit, `pose_026` was removed
from both image folders because its right-view reprojection error was about
`3.998 px`.

### Stereo calibration command used for the final result

```powershell
.\venv\Scripts\python.exe stereo_camera_calibrate.py `
  --left-images stereo_checkerboard_blink5\left\calib_images_no_pose026 `
  --right-images stereo_checkerboard_blink5\right\calib_images_no_pose026 `
  --left-intrinsics checkerboard_calib_blink5_cam2\silky_single_calibration_final_square7p1.npz `
  --right-intrinsics checkerboard_calib_blink5_cam1\silky_single_calibration_final_square7p1.npz `
  --square-mm 7.1 `
  --output stereo_checkerboard_blink5\stereo_calibration_square7p1_cam2left_cam1right_no_pose026.npz `
  --debug-dir stereo_checkerboard_blink5\debug_cam2left_cam1right_no_pose026
```

Do not overwrite the existing final NPZ when experimenting.  Change `--output`
and `--debug-dir` to names ending in `_trial`.

### Outputs and evaluation

| Output | Meaning | How it is used next |
| --- | --- | --- |
| `stereo_calibration_square7p1_cam2left_cam1right_no_pose026.npz` | Final left/right intrinsics, `R`, `T`, rectification data, and stereo RMS | Required by triangulation and camera-frame CAD alignment |
| Same-name CSV | Pose acceptance report | Check matched left/right board detections |
| `debug_cam2left_cam1right_no_pose026/` | Detection overlays | Inspect rejected or suspicious image pairs |
| `stereo_calibration_parameters_summary.txt` | Human-readable summary | Inspection and backup |

Final result:

- valid stereo pairs: `29 / 44`
- stereo RMS: `0.323912 px`
- optical-centre baseline: `120.385 mm`

The RMS is the common left/right checkerboard reprojection residual after
fitting the two-camera geometry.  The mechanical C/CS reference distance of
about `125 mm` is a different measurement and is not substituted for the
calibrated optical-centre baseline.

## 5. Step C: record a particle trajectory (completed example)

### Input

- Both connected cameras.
- The final stereo calibration NPZ from step B.
- A levitated particle and an AcousTools trajectory or static PAT target.

### Recording command

The script first opens a sequential dual-camera preview.  Set the particle so
it is visible in both views, accept the preview, then it records paired NPZ
files using a shared PC-clock start.

```powershell
.\venv\Scripts\python.exe stereo_eventcam_record.py `
  --left-serial 00000508 `
  --right-serial 00000509 `
  --duration-sec 2.0 `
  --delta-t-us 1000 `
  --start-delay-sec 2.0 `
  --stereo-calibration stereo_checkerboard_blink5\stereo_calibration_square7p1_cam2left_cam1right_no_pose026.npz `
  --note "heart trajectory"
```

### Outputs and evaluation

The command creates a timestamped run directory such as:

```text
stereo_eventcam_records/stereo_event_YYYYMMDD_HHMMSS/
  stereo_recording_manifest.json
  left/left_events.npz
  right/right_events.npz
```

The manifest is the authoritative record of serials, requested duration,
event counts, start timing, and the calibration path.  The two event streams
are started from a shared PC-clock target; this is not hardware event-timestamp
synchronization.  Any estimated left/right time offset is supplied later to
processing as `--right-time-offset-sec`.

Completed example:

```text
stereo_eventcam_records/stereo_event_20260716_175740/
```

This run recorded 2.0 s from both cameras and is the source of the current
heart-trajectory camera-frame result.

## 6. Step D: 2D tracking and 3D triangulation (completed example)

### Input

- One run directory from step C.
- The final stereo calibration NPZ from step B.

### Standard processing command for a moving particle

The current script defaults are the relaxed detector settings chosen for short
windows.  This command states them explicitly and also generates visual
overlay videos for quality control.

```powershell
.\venv\Scripts\python.exe stereo_process_recording.py `
  stereo_eventcam_records\stereo_event_20260716_175740 `
  --stereo-calibration stereo_checkerboard_blink5\stereo_calibration_square7p1_cam2left_cam1right_no_pose026.npz `
  --window-us 100 `
  --hop-us 50 `
  --dt-us 50 `
  --threshold-count 1 `
  --min-events 20 `
  --min-area 5 `
  --min-mass 30 `
  --render-overlay `
  --overlay-fps 1000 `
  --overlay-accumulation-us 1000 `
  --overlay-trail-sec 0.02
```

The historical heart run was processed earlier with `window_us=500`,
`hop_us=500`, and `dt_us=500`; this is recorded in each side's
`event_tracking_meta.json`.  It is a valid result, but it is not the current
100 us / 50 us default for faster motion.

### Outputs and checks

```text
RUN/
  left/event_tracking/
    event_centres_raw.csv
    event_centres_interp.csv
    event_tracking_summary.json
    xy.png
    t_xy.png
  right/event_tracking/
    ... same structure ...
  left/left_events_raw_overlay.mp4
  right/right_events_raw_overlay.mp4
  stereo_3d/
    stereo_3d_points.npz
    stereo_3d_points.csv
    stereo_3d_points_summary.json
    stereo_3d_trajectory.png
```

Evaluate the result in this order:

1. Open each overlay video.  The marker should stay on the same particle and
   should not jump to a bright background event.
2. Read both `event_tracking_summary.json` files.  Check missing/rejected
   points and the event/component statistics.
3. Read `stereo_3d_points_summary.json`.  Check `input_points`,
   `valid_points`, the time-gap setting, and the reconstructed ranges.
4. Inspect `stereo_3d_trajectory.png` or the interactive viewer.

For the completed heart example, the 3D stage accepted `3998 / 3999` samples.
Its output is still in the left-camera frame, not the PAT frame.

### Triangulation-only command

Use this only after 2D tracks already exist and need to be re-triangulated.

```powershell
.\venv\Scripts\python.exe stereo_triangulate_tracks.py `
  --left-track RUN\left\event_tracking\event_centres_interp.csv `
  --right-track RUN\right\event_tracking\event_centres_interp.csv `
  --stereo-calibration stereo_checkerboard_blink5\stereo_calibration_square7p1_cam2left_cam1right_no_pose026.npz `
  --output-dir RUN\stereo_3d
```

## 7. Step E: inspect the camera-frame result (completed)

### Static plot

```powershell
.\venv\Scripts\python.exe stereo_plot_3d_points.py `
  stereo_eventcam_records\stereo_event_20260716_175740\stereo_3d\stereo_3d_points.npz `
  --range-mode calibration
```

### Interactive viewer with the camera-frame CAD model

```powershell
.\venv\Scripts\python.exe stereo_3d_viewer.py `
  --input stereo_eventcam_records\stereo_event_20260716_175740\stereo_3d\stereo_3d_points.npz `
  --cad-obj drawings\openmpd_case_Twin_20260717.obj `
  --cad-config drawings\openmpd_case_Twin_20260717_camera_overlay.json `
  --bind 127.0.0.1 `
  --port 8774
```

Open `http://127.0.0.1:8774/?cad=1`.  The camera-frame CAD overlay is useful
for confirming camera identity and mechanical context.  `Anchor error L / R`
should be `L 0.00 / R 0.00 mm` after `Reset CAD`; that verifies the CAD lens
anchors against the stereo camera centres.  It does not establish PAT axes.

## 8. Step F: measure the camera-to-PAT transform (next required task)

This is the missing step.  It replaces a guessed CAD alignment with an
empirical rigid transform:

```text
p_pat_mm = R_camera_to_pat * p_left_camera_mm + t_camera_to_pat_mm
```

### Input

- Final stereo calibration from step B.
- PAT target positions known in PAT coordinates.
- Static-particle stereo recordings at non-coplanar PAT positions.
- `pat_stereo_grid_config.json`, currently a 27-point grid centred on
  `[0, 0, 120] mm` with X/Y/Z offsets of `[-10, 0, +10]`, `[-5, 0, +5]`, and
  `[-10, 0, +10]`.

The particle must settle at every target.  The grid spans all three axes so a
full 3D rigid transform can be estimated.

### Commands

First validate the planned hardware and PAT path without opening devices:

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_capture.py --dry-run
```

Capture the grid.  This creates a timestamped session under
`pat_stereo_grid_records/`.

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_capture.py
```

Then process the session.  Replace `SESSION_DIR` with the directory printed by
the capture command.

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_process.py SESSION_DIR
```

For a partial session that already has 3D output for every accepted point:

```powershell
.\venv\Scripts\python.exe pat_stereo_grid_process.py SESSION_DIR --skip-processing
```

### Outputs and acceptance checks

```text
SESSION_DIR/
  pat_stereo_grid_manifest.json
  captures/
  registration/
    camera_to_pat_transform.npz
    camera_to_pat_transform.json
    camera_to_pat_transform_summary.txt
    grid_correspondences.csv
    pat_camera_registration.png
```

Before accepting the transform, inspect:

1. `usable_points`: enough well-tracked grid points, distributed over X/Y/Z.
2. `target_rank = 3`: the PAT targets span 3D and are not coplanar.
3. `fit_rms_mm` and `validation_rms_mm`: registration residuals in mm.
4. `similarity_scale_diagnostic`: should be close to `1.0`; the saved
   transform remains rigid and never applies this scale.
5. `grid_correspondences.csv`: rejected points, per-point particle scatter,
   and per-point registration residual.

This residual includes the actual static PAT equilibrium error, event tracking
noise, stereo triangulation error, and any mismatch in the commanded PAT
positions.  It is therefore the first operational mm-accuracy measurement.

## 9. Step G: transform a trajectory into PAT coordinates (after step F)

### Input

- Camera-frame `stereo_3d_points.npz` from step D.
- `camera_to_pat_transform.npz` from step F.

### Command

```powershell
.\venv\Scripts\python.exe stereo_apply_pat_transform.py `
  stereo_eventcam_records\stereo_event_20260716_175740\stereo_3d\stereo_3d_points.npz `
  --transform SESSION_DIR\registration\camera_to_pat_transform.npz `
  --relative-start
```

### Outputs and use

```text
stereo_3d_points_pat.npz
stereo_3d_points_pat.csv
stereo_3d_points_pat_summary.json
```

`points_pat_mm` is the absolute PAT-frame trajectory.  `--relative-start`
also records displacement from the median of the initial stable 0.05 s.  This
is useful for relative trajectory shape comparison, but it is not a substitute
for the rigid transform.

The next analysis step is time alignment with the AcousTools ideal log, then
comparison of the ideal PAT trajectory and measured PAT trajectory.  That
comparison should retain both absolute position residual and relative-start
displacement residual, because PAT static offset and dynamic tracking error are
different phenomena.

## 10. Files to preserve

Minimum calibration backup:

```text
checkerboard_calib_blink5_cam2/silky_single_calibration_final_square7p1.npz
checkerboard_calib_blink5_cam1/silky_single_calibration_final_square7p1.npz
stereo_checkerboard_blink5/stereo_calibration_square7p1_cam2left_cam1right_no_pose026.npz
stereo_checkerboard_blink5/stereo_calibration_parameters_summary.txt
```

For reproducibility, preserve the two checkerboard capture directories, the
stereo checkerboard directory, all raw trajectory run directories, the PAT grid
session directory, and this source set:

```text
eventcam_checkerboard_calibration_capture.py
single_camera_calibrate.py
stereo_checkerboard_calibration_capture.py
stereo_camera_calibrate.py
stereo_eventcam_record.py
stereo_process_recording.py
stereo_triangulate_tracks.py
pat_stereo_grid_capture.py
pat_stereo_grid_process.py
stereo_apply_pat_transform.py
stereo_3d_viewer.py
stereo_3d_viewer/
pat_stereo_grid_config.json
drawings/
```

## 11. Ossila stage depth/shape accuracy experiment

The separate workflow in `STEREO_STAGE_ACCURACY_WORKFLOW_JP.md` evaluates
stereo displacement accuracy along a measured stage axis and local 3D shape
accuracy from the eight PAT cuboid vertices.  It uses:

```text
stereo_stage_accuracy_capture.py
stereo_stage_accuracy_analyze.py
stereo_stage_accuracy_common.py
stereo_stage_accuracy_config.json
test_stereo_stage_accuracy.py
```

Run the capture command without `--execute` first.  That mode validates and
writes the complete motion/capture plan without opening the stage, PAT, or
cameras.  Registration uses its own reference-station pass; five forward/reverse
cycles are evaluated separately using Ossila readback differences, so opposite
errors cannot be hidden by pooling.
