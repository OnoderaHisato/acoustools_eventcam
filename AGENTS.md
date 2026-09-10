# Project Instructions for Codex

Before doing any work, read `PROJECT_HANDOFF.md`. For detailed behavior and commands, also read:

- `STEREO_ACOUSTOOLS_3D_AUTO_JP.md`
- `STEREO_ACOUSTOOLS_3D_PIPELINE_JP.md`

## Safety and Scope

- This project controls real AcousTools/OpenMPD and stereo event-camera hardware.
- Do not start PAT output, connect to or move the levitated particle, open cameras, or run recording/pipeline entry points unless the user explicitly asks for that hardware action.
- Prefer read-only inspection, `--help`, or documented hardware-free `--dry-run` validation when diagnosing code.
- Do not run heavy particle extraction/postprocessing locally unless the user explicitly requests it. The current plan is to transfer pending captures to a supercomputer.
- Preserve all existing measurement artifacts under `stereo_acoustools_3d_records_auto/`. Do not delete or overwrite NPZ, CSV, JSON, image, or video outputs without explicit target approval.
- Never silently change camera serials, left-master synchronization, the left LED detector, ROI `600,0,1280,180`, or the stereo calibration path.
- Failed LED captures are deleted by default and should prompt for recapture. Use `--keep-failed-captures` only for an explicitly requested diagnosis.

## Working Conventions

- Use `.\venv\Scripts\python.exe` for local Python commands.
- Scaled trajectory sets are available at 95%, 90%, 85%, and 80%. Use the exact file requested by the user; do not silently choose a scale.
- Keep recording and heavy postprocessing separate unless the user explicitly asks for the combined pipeline.
- Run targeted root-level tests only. Exclude `venv/`, `_external_repos/`, calibration captures, and measurement output trees from broad searches/test collection.
- Preserve unrelated user changes and generated data.
- Keep Markdown files UTF-8 encoded.
- If a material implementation or measurement-state change is made, update `PROJECT_HANDOFF.md` before handing off.
- This directory is a Git repository, initialized at the user's request on 2026-09-10. Its remote is `https://github.com/OnoderaHisato/acoustools_eventcam.git` (public). Keep the code-only allowlist in `.gitignore`; never force-add measurement artifacts, phase maps, dependencies, or credentials. Commit/push only when requested by the user.
