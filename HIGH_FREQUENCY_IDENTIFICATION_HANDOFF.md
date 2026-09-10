# High-frequency identification handoff

## Objective

Measure the command-to-particle response in the unexplained 70--110 Hz band
before fitting a control model.  These recordings are identification inputs,
not validation of the existing delay model and not yet a SINDy experiment.

## Measurement-PC status (2026-08-17)

The measurement-PC integration is complete. The existing automatic stereo
recorder now accepts `--hf-export-dir`, applies the exported offset around the
current levitation centre, writes the absolute `ideal_log.csv`, and preserves
the exact command artifacts plus hashes in each run. Camera serials,
left-master synchronization, the left LED ROI, and calibration defaults were
not changed. Delay feedforward remains disabled.

Twelve enabled commands were exported under `hf_identification_export/`. The
full configured set and the first 50% x-chirp both passed hardware-free
dry-runs. All previews were inspected. The Z-axis diagnostic remains disabled.

The initial 50% x-chirp retained a visible particle in every lightweight frame,
but the old capture margin omitted the final approximately 0.197 s. The default
tail margin is now 2.0 s. Ordered `--hf-run LABEL=SCALE` requests support a
corrected 50% capture followed by 100% in one persistent hardware session.

## Required measurement-PC files

The files are now present together in the measurement workspace. At minimum,
preserve these files when moving the workflow to another PC:

- `hf_identification_trajectory.py`
- `high_frequency_identification_plan.json`
- `stereo_acoustools_3d_hf_common.py`
- `acoustools_stereo_eventcam_3d_recording_auto.py`
- `stereo_acoustools_3d_recording_core.py`
- stereo calibration and the existing camera/PAT environment

## Generate and inspect commands without hardware

From the measurement workspace root:

```powershell
.\venv\Scripts\python.exe .\hf_identification_trajectory.py `
  --output-dir .\hf_identification_export `
  --write-csv
```

Inspect every `trajectory_preview.png` and `trajectory_metadata.json`.  The NPZ
contains `offset_mm`, `offset_m`, `time_sec`, and `sample_hz`; both coordinate
arrays are offsets from the levitation centre.  The hardware code must use:

```python
from hf_identification_trajectory import load_trajectory_for_hardware

cycle_positions, sample_hz, trajectory_meta = load_trajectory_for_hardware(
    run_dir / "command_trajectory.npz",
    center_m=current_static_pos,
    command_scale=0.5,  # first retention/visibility trial only
)
```

Then pass `cycle_positions` through the existing AcousTools hologram calculation
and PAT send path.  Preserve the existing stereo hardware sync and PAT-start LED
recording.  Write the absolute `cycle_positions` with the existing recorder's
`ideal_log.csv` function.  `command_offset_log.csv` is deliberately not named
`ideal_log.csv`, because it does not contain absolute PAT coordinates.  Store
`trajectory_meta` in `pipeline_manifest.json`, including the applied scale.

## Required experimental order

1. Run `00_static_baseline` twice.  This measures camera/event-processing noise;
   it does not provide a continuously tracked stationary trajectory by itself.
2. Run x chirp once at 50% command scale, inspect particle retention and event
   visibility, then run the configured x/y chirps.
3. Run x/y multisines.  Repeats intentionally use different phases so a peak is
   not tied to one crest factor.
4. Repeat at least one chirp on a different day or after re-levitation.
5. Keep `90_z_chirp_220_350hz_diagnostic` disabled until the stereo noise floor
   is below its several-micrometre command amplitude and retention is confirmed.

## Measurement-PC commands

Record the two static baselines first:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\hf_identification_export `
  --output-dir .\stereo_acoustools_3d_records_hf `
  --label 00_static_baseline_r01 `
  --label 00_static_baseline_r02 `
  --confirm-each-run
```

Then record only the first x chirp at 50%:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\hf_identification_export `
  --output-dir .\stereo_acoustools_3d_records_hf `
  --label 10_x_chirp_40_130hz_r01 `
  --hf-command-scale 0.5 `
  --confirm-each-run
```

Inspect the resulting left/right lightweight preview. Only after retention and
visibility are confirmed, run full-scale dynamic commands with
`--acknowledge-hf-retention-and-visibility`. Do not start the heavy local
postprocessor merely for this inspection.

After the initial retention/visibility check has passed, repeat the complete
50% chirp and then run 100% in one session:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_eventcam_3d_recording_auto.py `
  --hf-export-dir .\hf_identification_export `
  --output-dir .\stereo_acoustools_3d_records_hf `
  --hf-run 10_x_chirp_40_130hz_r01=0.5 `
  --hf-run 10_x_chirp_40_130hz_r01=1.0 `
  --acknowledge-hf-retention-and-visibility `
  --confirm-each-run
```

The 2.0 s tail margin is now the recorder default, so the command does not need
an explicit `--capture-tail-margin-sec` option.

Do not enable delay feedforward for identification.  We need the unmodified
command as input and the measured particle position as output.  Applying the
current delay compensation would mix the controller with the plant response.

## Safety checks before PAT send

The generator numerically checks vector displacement, speed, and acceleration
and scales the complete command if any configured limit is exceeded.  This is a
software bound, not a guarantee that the particle remains trapped.  Confirm the
centre position, acoustic power, apparatus clearance, and emergency stop on the
measurement PC.  Start at 50% amplitude because the old 28,000 mm/s^2 limit is
empirical and does not establish high-frequency stability.

## Files to bring back

Bring back each complete run directory, including:

- `pipeline_manifest.json`
- `trajectory_metadata.json` and the exact `command_trajectory.npz`
- the hardware recorder's absolute-coordinate `ideal_log.csv`
- `command_offset_log.csv`
- `stereo_recording/left/left_events.npz`
- `stereo_recording/right/right_events.npz`
- PAT/LED timing files and stereo calibration identifier

Do not transfer only reconstructed 3D CSV files.  Raw event data is needed to
test whether the 70--110 Hz feature changes with tracking parameters.

## Continue with Codex on the other PC

Codex conversation state does not automatically move between machines.  Open
the transferred repository on the measurement PC and start with this prompt:

> Read `Acoutools_eventcam/HIGH_FREQUENCY_IDENTIFICATION_HANDOFF.md` and
> `Acoutools_eventcam/high_frequency_identification_plan.json`.  First inspect
> the local measurement-only stereo recording scripts and integrate the NPZ
> offset trajectories without changing camera hardware sync or LED timing.
> Run a dry-run/export and report the exact safety metrics before opening PAT
> hardware.  Do not apply delay feedforward during identification.

After acquisition, use the same handoff file when reopening Codex on this PC and
ask it to inspect the returned run directories before fitting FRF, delay, or
SINDy models.
