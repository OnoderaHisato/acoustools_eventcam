# Heart trajectory delay-feedforward A/B test

This experiment uses dedicated helper module names so it can be placed beside
the existing stereo workflow without replacing its default core files:

- `heart_delay_feedforward.py`
- `stereo_acoustools_heart_delay_recording_core.py`
- `stereo_acoustools_heart_delay_common.py`

Keep these files in the same directory as `acoustools_stereo_heart_delay_ab.py`.
The standard AcousTools/event-camera modules and the machine-specific stereo
calibration must already be available in the execution environment.

## Purpose

Apply the already identified common delay, `tau = 2.05 ms`, to a previously
unused 7 mm, 10 Hz heart trajectory without fitting any heart-specific model.
The primary error is measured particle position minus the original heart
reference `r(t)`, not measured position minus the intentionally advanced PAT
command `u(t)`.

## Control equation

```text
u(t) = r(t) + tau * dr(t)/dt
```

The command positions are converted to holograms by the existing
`compute_holograms_for_positions` function.  Delay compensation is therefore
applied before hologram calculation, while the camera and PAT timing path is
unchanged.

## Dry run first

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_heart_delay_ab.py --dry-run
```

For 800 points/cycle, 10 Hz, 7 mm, and `tau=2.05 ms`, the PAT update rate is
8,000 frames/s (40 kHz divider 5) and ten loops take 1.0 s.  The unconstrained
command requires approximately 1.527 mm maximum lead.  The default 1 mm limit uses
smooth global scaling, giving an effective delay compensation of approximately
1.342 ms.  It is a safety-constrained controller, not the exact inverse.

Pointwise clipping is available for comparison but is not recommended because
its transitions add acceleration harmonics:

```powershell
--delay-limit-strategy pointwise_clip
```

## Initial bounded A/B acquisition

The following records three pairs.  Pair order is counterbalanced automatically
to reduce drift bias: baseline/delay, delay/baseline, baseline/delay.

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_heart_delay_ab.py `
  --heart-scale-mm 7 `
  --heart-frequency-hz 10 `
  --steps-per-cycle 800 `
  --loops 10 `
  --repeats 3 `
  --delay-tau-ms 2.05 `
  --delay-max-offset-mm 1.0 `
  --delay-limit-strategy global_scale `
  --confirm-each-run
```

The original 7 mm, 10 Hz heart already has approximately 93,400 mm/s^2 peak
command acceleration at this discretization.  It was used previously, but that
history is not a safety guarantee.  Confirm particle retention and visibility
before running all six acquisitions.

## Exact model-inverse experiment

Only after confirming apparatus clearance and particle retention, the 1.6 mm
limit avoids limiting this specific discretized command:

```powershell
.\venv\Scripts\python.exe .\acoustools_stereo_heart_delay_ab.py `
  --delay-max-offset-mm 1.6 `
  --delay-limit-strategy global_scale `
  --require-unclipped-delay `
  --acknowledge-delay-offset-above-1mm `
  --confirm-each-run
```

Do not silently increase this limit.  The acknowledgement is intentionally
required above 1 mm.

## Output semantics

Each run contains:

- `*_ideal_log.csv`: original heart reference `r(t)` used by existing 3D
  postprocessing and tracking-error metrics.
- `*_command_log.csv`: exact PAT command `u(t)` plus `reference_*` columns.
- `*_command_trajectory_preview.png`: command sent for hologram generation.
- `*_reference_trajectory_preview.png`: desired heart.
- `pipeline_manifest.json`: control mode, tau, limit strategy, effective tau,
  maximum lead, and limiting fraction.

For both conditions, evaluate `measured - reference`.  The first cycle may
contain initialization transient; report all-cycle metrics and a second metric
excluding the first cycle.  Do not refit tau using these heart runs when
claiming zero-shot generalization.

After all run directories have completed the existing stereo 3D postprocess,
aggregate the paired results with:

```powershell
.\venv\Scripts\python.exe .\compare_heart_delay_ab.py `
  .\stereo_acoustools_heart_delay_ab_records\heart_delay_ab_session_YYYYMMDD_HHMMSS.json
```

The aggregator excludes the first cycle by default and writes per-run CSV,
paired improvement CSV, summary JSON, and a paired RMSE plot.  Use
`--include-first-cycle` only for a separate initialization-sensitive result.
