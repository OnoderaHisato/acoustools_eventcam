# Project Instructions for Claude Code

@PROJECT_HANDOFF.md
@STEREO_ACOUSTOOLS_3D_AUTO_JP.md
@STEREO_ACOUSTOOLS_3D_PIPELINE_JP.md

## Mandatory Operating Rules

- Read the imported handoff and stereo workflow documents before changing code.
- This workspace controls real AcousTools/OpenMPD and stereo event-camera hardware. Do not start PAT output, move a levitated particle, open cameras, or run recording/pipeline entry points unless the user explicitly requests that hardware action.
- Prefer read-only inspection and hardware-free dry runs for diagnosis.
- Do not run computationally heavy particle extraction locally unless explicitly requested; pending captures are intended for supercomputer processing.
- Preserve all existing artifacts under `stereo_acoustools_3d_records_auto/`. Do not overwrite or delete measurement data without explicit target approval.
- Do not silently change camera serials, left-master synchronization, the left-camera LED detector, ROI `600,0,1280,180`, or the calibration file.
- Use `.\venv\Scripts\python.exe` for local Python commands.
- Scaled trajectory sets are available at 95%, 90%, 85%, and 80%. Use the exact file requested by the user; do not silently choose a scale.
- Run targeted root-level tests only; exclude `venv/`, `_external_repos/`, calibration capture trees, and measurement outputs from recursive test discovery.
- Keep `PROJECT_HANDOFF.md` current after material implementation or measurement-state changes.
- This directory is a Git repository, initialized at the user's request on 2026-09-10. Its remote is `https://github.com/OnoderaHisato/acoustools_eventcam.git` (public). Keep the code-only allowlist in `.gitignore`; never force-add measurement artifacts, phase maps, dependencies, or credentials. Commit/push only when requested by the user.
