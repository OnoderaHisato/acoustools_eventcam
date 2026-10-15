"""One-minute environment table (env_timeline.py) and its writing at the end of an auto session."""

from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import env_timeline
from test_thermal_hold_test import run_main, write_export

T0 = 1_800_000_000.0  # a whole minute


def write_logs(root: Path) -> tuple[Path, Path, Path]:
    env = root / "env.csv"
    lines = ["time_iso,time_unix_s,received_unix_s,temperature_C,humidity_percent,pressure_hPa,dew_point_C,error"]
    lines += [f"x,{T0 + 10 * i},{T0 + 10 * i},24.{i % 10},50,1010,13," for i in range(12)]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pat = root / "pat.csv"
    lines = ["time_iso,time_unix_s,port,cycle,arduino_ms,rom,name,group,temp_raw_C,offset_C,temp_C,status,error,event"]
    for i in range(150):
        for group, value in (("top", 30), ("bottom", 31)):
            lines.append(f"x,{T0 + i},COM12,{i},0,R{group},n{group},{group},{value},0,{value},ok,,")
    pat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    psu = root / "psu.csv"
    lines = ["time_iso,time_unix_s,voltage_V,current_A,power_W,output_on,voltage_set_V,current_set_A,error"]
    lines += [f"x,{T0 + i},15,{4.3 if i == 70 else 4.2},63,1,15,49," for i in range(170)]
    psu.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env, pat, psu


class EnvTimelineTests(unittest.TestCase):
    def test_minutes_sources_and_runs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            env, pat, psu = write_logs(root)
            out = root / "timeline.csv"
            summary = env_timeline.build_timeline(
                out, start_unix=T0 + 5, end_unix=T0 + 175, env_log=env, pat_temp_log=pat, psu_log=psu,
                sound_on_unix=T0, runs=[{"number": 1, "label": "kx", "start_unix": T0 + 50, "end_unix": T0 + 70}])
            self.assertEqual(summary["minutes"], 3)
            with out.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r["runs"] for r in rows], ["1:kx", "1:kx", ""])
            self.assertEqual((rows[0]["top_pat_C"], rows[0]["bottom_pat_C"], rows[0]["pat_n"]), ("30.0", "31.0", "120"))
            self.assertEqual(rows[1]["supply_A_max"], "4.3")
            self.assertEqual(rows[2]["env_n"], "0")              # the Tempo Disc rows end at 110 s
            self.assertEqual(rows[1]["minutes_after_sound_on"], "1.0")

    def test_missing_logs_leave_empty_columns(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = env_timeline.build_timeline(root / "t.csv", start_unix=T0, end_unix=T0 + 60,
                                                  env_log=root / "none.csv")
            self.assertEqual((summary["minutes"], summary["minutes_with_env"]), (1, 0))

    def test_session_end_writes_the_table(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            _env, _pat, psu = write_logs(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"), "--hf-run", "kx=1",
                    "--unattended-after-first-checkpoint", "--acknowledge-step-response-risk", "--no-notify",
                    "--psu-log", str(psu)]
            result, _events, _ = run_main(argv)
            self.assertEqual(result, 0)
            session = json.loads(next((root / "rec").glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
            self.assertTrue(Path(session["env_timeline_csv"]).is_file())


if __name__ == "__main__":
    unittest.main()
