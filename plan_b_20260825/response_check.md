# Plan B response gate

Source: `G:\マイドライブ\Experiment\20260826\plan_b_20260825\response_check.md` (2026-08-25).

The preregistered model is
`x'' = -w0^2 g(x-u(t-1.4 ms)) - gamma0 x' - c|x'|x'`.
The practical force-peak/escape limit is 1.072 mm. A predicted peak
`|x-u| > 0.6 mm` is marked HOLD.

HOLD runs:

- `13_y_multisine_rung20.0um_r01`
- `13_y_multisine_rung20.0um_r02`
- `13_z_multisine_rung05.0um_r01`
- `14_x_multisine_rung35.0um_r01`
- `14_x_multisine_rung35.0um_r02`
- `14_y_multisine_rung35.0um_r01`
- `14_y_multisine_rung35.0um_r02`

Run a HOLD command only after every lower escalation rank retained the same
particle and remained visible in both cameras. The recorder requires
`--acknowledge-plan-b-hold-runs` for these exact exports.

All B2 commands were GO in the source response simulation. This prediction is
not a retention guarantee; the per-run preview and Enter checkpoint remain
mandatory.
