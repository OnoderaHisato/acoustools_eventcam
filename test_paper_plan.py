"""Paper sessions S1-S4 (paper_20261014/paper_plan.json, runlist), the end-of-session particle hold,
the notifications and the particle record. Hardware-free."""

from __future__ import annotations

import csv
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np

import acoustools_stereo_eventcam_3d_recording_auto as auto_record
import particle_prompt
import particle_record
import session_notify
from hf_identification_trajectory import array_sha256
from stereo_acoustools_3d_recording_core import safe_run_dir_base
from test_thermal_hold_test import run_main, write_export

ROOT = Path(__file__).resolve().parent
CAMPAIGN = ROOT / "paper_20261014"
PLAN = CAMPAIGN / "paper_plan.json"
DEFAULT_OUTPUT_ROOT = ROOT / "stereo_acoustools_3d_records_V15c"
NEW_DIRS = {"ff_paper": 159, "w_scan_yoffset": 24, "kgrid_paper": 6, "w_scan_dense": 6}


def runlist_rows() -> list[dict]:
    rows = []
    for s in ("S1", "S2", "S3", "S4"):
        with open(CAMPAIGN / "runlist" / f"{s}_runlist.csv", encoding="utf-8-sig", newline="") as f:
            rows.extend(csv.DictReader(f))
    return rows


@unittest.skipUnless(PLAN.is_file(), "paper plan not present")
class PaperPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = json.loads(PLAN.read_text(encoding="utf-8"))
        cls.exps = cls.plan["experiments"]
        cls.by_name = {e["name"]: e for e in cls.exps}

    def test_counts_and_design_dirs(self):
        self.assertEqual(len(self.exps), 296)
        self.assertEqual(len(self.by_name), 296)
        dirs = [e.get("feedforward_design", {}).get("design_dir") for e in self.exps]
        for d, n in NEW_DIRS.items():
            self.assertEqual(dirs.count(d), n, d)
        shifted = [e for e in self.exps if "source/ff_old_extra/" in e.get("source_npz", "")]
        self.assertEqual(len(shifted), 6)
        for e in shifted:
            self.assertEqual(e["feedforward_design"]["design_dir"], "ff_heart_15V")
        reused = [e for e in self.exps if "copied_from" in e]
        self.assertEqual(len(reused), 95)
        for e in self.exps:
            self.assertEqual(e["campaign"], "paper_20261014", e["name"])
            if e["kind"] == "imported":
                self.assertIn(e["feedforward_design"]["design"], ("OFF", "HOLD", "WSCAN"), e["name"])   # no compensation in S1-S4

    def test_every_runlist_row_is_in_the_plan(self):
        rows = runlist_rows()
        self.assertEqual(len(rows), 494)
        changes = [r for r in rows if r["kind"] == "change"]
        self.assertEqual([(r["session"], r["order"], r["particle"]) for r in changes],
                         [("S3", "102", "1"), ("S4", "1", "2"), ("S4", "45", "3")])
        names = {r["run"] for r in rows if r["kind"] != "change"}
        self.assertEqual(names, set(self.by_name))
        per_session = {s: sum(1 for r in rows if r["session"] == s and r["kind"] != "change") for s in ("S1", "S2", "S3", "S4")}
        self.assertEqual(per_session, {"S1": 115, "S2": 146, "S3": 144, "S4": 86})
        for r in rows:
            if r["kind"] == "change":
                continue
            entry = self.by_name[r["run"]]
            if entry["kind"] != "imported":
                continue                                   # kcheck staircases carry no source file
            src = entry.get("feedforward_design", {}).get("source_file", "") or Path(entry["source_npz"]).name
            self.assertTrue(src.startswith(r["source_file"]), (r["run"], src, r["source_file"]))

    def test_commands_pinned_and_within_limits(self):
        limits = self.plan["defaults"]["safety_limits"]
        self.assertEqual(limits["max_offset_mm"], 35.0)
        self.assertEqual(limits["max_acceleration_mm_s2"], 350000.0)
        worst = {"offset": 0.0, "speed": 0.0, "accel": 0.0}
        for e in self.exps:
            if e["kind"] != "imported":
                continue
            with np.load(CAMPAIGN / e["source_npz"], allow_pickle=False) as z:
                pos = np.array(z["positions_mm"], dtype=float)
                ref = np.array(z["reference_mm"], dtype=float)
                hz = float(z["sample_hz"])
            self.assertEqual(array_sha256(pos), e["positions_sha256"], e["name"])
            self.assertEqual(hz, 10000.0)
            self.assertAlmostEqual(pos.shape[0] / hz, e["duration_sec"], places=6)
            vel = np.gradient(pos, 1.0 / hz, axis=0)
            acc = np.gradient(vel, 1.0 / hz, axis=0)
            worst["offset"] = max(worst["offset"], float(np.linalg.norm(pos, axis=1).max()))
            worst["speed"] = max(worst["speed"], float(np.linalg.norm(vel, axis=1).max()))
            worst["accel"] = max(worst["accel"], float(np.linalg.norm(acc, axis=1).max()))
            self.assertLessEqual(np.linalg.norm(pos - ref, axis=1).max(), limits["max_command_reference_distance_mm"])
            self.assertLessEqual(max(np.linalg.norm(pos[0]), np.linalg.norm(pos[-1])), limits["max_endpoint_offset_mm"])
        self.assertLess(worst["offset"], limits["max_offset_mm"])
        self.assertLess(worst["speed"], limits["max_speed_mm_s"])
        self.assertLess(worst["accel"], limits["max_acceleration_mm_s2"])
        self.assertGreater(worst["accel"], 230000.0)      # heart_s7_f16 / rotline_r23_d12_f11 (README: 239k)

    def test_rotline_and_dense_scan_entries(self):
        rot = [e for e in self.exps if e["name"].startswith("rotline_")]
        self.assertEqual(len(rot), 16)
        for e in rot:
            with np.load(CAMPAIGN / e["source_npz"], allow_pickle=False) as z:
                pos = np.array(z["positions_mm"], dtype=float)
            step = np.linalg.norm(np.diff(pos, axis=0), axis=1).max()
            self.assertLess(step, 3.2, e["name"])               # the staircase jump limit never applies, but check anyway
        dense = [e for e in self.exps if e["name"].endswith("R28d_a") or e["name"].endswith("R28d_b")]
        self.assertEqual(len(dense), 6)
        for e in dense:
            self.assertEqual(e["duration_sec"], 48.0)
            self.assertEqual(e["feedforward_design"]["design_dir"], "w_scan_dense")

    def test_run_names_fit_the_folder_name(self):
        for index, entry in enumerate(self.exps):
            self.assertLessEqual(len(entry["name"]), 27, entry["name"])
            shape = "step_response_identification" if entry["kind"] == "staircase" else "feedforward_validation"
            label = f"{index:03d}_{entry['name']}_scale100_V15"
            base = safe_run_dir_base(DEFAULT_OUTPUT_ROOT, shape, label, "20261016_010203")
            self.assertEqual(base, f"{shape}_{label}_20261016_010203", entry["name"])

    def test_export_matches_plan(self):
        manifest = CAMPAIGN / "export_paper" / "export_manifest.json"
        if not manifest.is_file():
            self.skipTest("export not generated")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["source_plan_sha256"], hashlib.sha256(PLAN.read_bytes()).hexdigest())
        self.assertEqual(len(data["runs"]), 296)


class ParticleHoldTests(unittest.TestCase):
    class Clock:
        def __init__(self):
            self.t = 1000.0

        def time(self):
            return self.t

        def sleep(self, s):
            self.t += s

    def test_enter_ends_the_hold_and_beeps_first(self):
        keys = iter([None, None, "\r"])
        beeps = []
        record = auto_record.hold_particle_for_collection(
            10.0, clock=self.Clock(), beeper=beeps.append, key_reader=lambda: next(keys, None))
        self.assertEqual(record["ended"], "enter")
        self.assertEqual(beeps[0], 3)

    def test_time_limit_and_ctrl_c(self):
        record = auto_record.hold_particle_for_collection(0.01, clock=self.Clock(), beeper=lambda n: None, key_reader=lambda: None)
        self.assertEqual(record["ended"], "timeout")
        self.assertGreaterEqual(record["held_sec"], 0.6)
        record = auto_record.hold_particle_for_collection(1.0, clock=self.Clock(), beeper=lambda n: None, key_reader=lambda: "\x03")
        self.assertEqual(record["ended"], "interrupt")

    def test_the_session_holds_before_the_single_shutdown(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                    "--hf-run", "kx=1", "--hf-run", "ky=1", "--unattended-after-first-checkpoint",
                    "--acknowledge-step-response-risk", "--hold-particle-until-enter", "--hold-particle-max-min", "5",
                    "--no-notify"]
            order = []
            hold = mock.Mock(side_effect=lambda *_a, **_k: (order.append("hold"), {"ended": "enter", "held_sec": 1.0})[1])
            with mock.patch.object(auto_record, "hold_particle_for_collection", hold),                     mock.patch.object(auto_record, "_session_can_hold", return_value=True):
                result, events, _ = run_main(argv)
                events.close_hw.side_effect = None
            self.assertEqual(result, 0)
            hold.assert_called_once_with(5.0)
            events.close_hw.assert_called_once()
            session = json.loads(next((root / "rec").glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
            self.assertEqual(session["status"], "complete")
            self.assertEqual(session["particle_hold"]["ended"], "enter")
            self.assertTrue(session["hold_particle_until_enter"])

    def test_the_hold_is_on_by_default(self):
        args = auto_record.parse_args(["--hf-export-dir", "x"])
        self.assertTrue(args.hold_particle_until_enter)
        args = auto_record.parse_args(["--hf-export-dir", "x", "--no-hold-particle-until-enter"])
        self.assertFalse(args.hold_particle_until_enter)

    def test_a_mocked_or_closed_session_never_holds(self):
        self.assertFalse(auto_record._session_can_hold(mock.sentinel.hardware_session))
        self.assertFalse(auto_record._session_can_hold(mock.Mock(lev=object(), is_shutdown=True)))
        self.assertTrue(auto_record._session_can_hold(mock.Mock(lev=object(), is_shutdown=False)))

    def test_switched_off_nothing_holds(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                    "--hf-run", "kx=1", "--unattended-after-first-checkpoint", "--acknowledge-step-response-risk",
                    "--no-notify", "--no-hold-particle-until-enter"]
            with mock.patch.object(auto_record, "hold_particle_for_collection") as hold,                     mock.patch.object(auto_record, "_session_can_hold", return_value=True):
                result, events, _ = run_main(argv)
            self.assertEqual(result, 0)
            hold.assert_not_called()
            events.close_hw.assert_called_once()

    def test_notification_at_the_end_and_at_a_particle_change(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            config = root / "notify.json"
            config.write_text(json.dumps({"event_dir": str(root / "events")}), encoding="utf-8")
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                    "--hf-run", "kx=1", "--hf-run", "ky=1", "--unattended-after-first-checkpoint",
                    "--particle-change-run-numbers", "2", "--acknowledge-step-response-risk",
                    "--notify-config", str(config)]
            with mock.patch("builtins.input", return_value=""):
                result, _events, _ = run_main(argv)
            self.assertEqual(result, 0)
            lines = (root / "events" / session_notify.EVENTS_FILE).read_text(encoding="utf-8").splitlines()
            events = [json.loads(line)["event"] for line in lines]
            self.assertEqual(events, ["particle_change", "session_complete"])
            latest = json.loads((root / "events" / session_notify.LATEST_FILE).read_text(encoding="utf-8"))
            self.assertIn("2/2 runs recorded", latest["summary"])
            self.assertIn("turned off", latest["summary"])

    def test_without_an_explicit_config_nothing_is_sent(self):
        # notify_config.json next to the scripts must never be picked up by tests or ad-hoc calls
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                    "--hf-run", "kx=1", "--unattended-after-first-checkpoint", "--acknowledge-step-response-risk"]
            with mock.patch.object(session_notify, "notify") as notify,                     mock.patch.object(session_notify, "load_config") as load:
                result, _events, _ = run_main(argv)
            self.assertEqual(result, 0)
            notify.assert_not_called()
            load.assert_not_called()

    def test_missing_config_is_refused_before_hardware(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                    "--hf-run", "kx=1", "--acknowledge-step-response-risk", "--notify-config", str(root / "none.json")]
            result, events, _ = run_main(argv)
            self.assertEqual(result, 2)
            events.open_hw.assert_not_called()


class NotifyTests(unittest.TestCase):
    def test_email_failure_is_a_warning_not_an_error(self):
        result = session_notify.notify("x", "y", config={"email": {"smtp_host": "127.0.0.1", "smtp_port": 1,
                                                                      "from": "a@b", "to": ["c@d"]}})
        self.assertFalse(result["email"])

    def test_password_comes_from_env_or_file_never_config(self):
        with TemporaryDirectory() as temporary:
            pw = Path(temporary) / "pw.txt"
            pw.write_text("secret\n", encoding="utf-8")
            self.assertEqual(session_notify._email_password({"password_file": str(pw)}), "secret")
            with mock.patch.dict("os.environ", {"X_PW": "fromenv"}):
                self.assertEqual(session_notify._email_password({"password_env": "X_PW", "password_file": str(pw)}), "fromenv")
            with mock.patch.object(session_notify, "_user_environment_value", return_value=None):
                self.assertIsNone(session_notify._email_password({"password": "inline"}))
            # a variable set after this terminal was opened is read from the user environment
            with mock.patch.dict("os.environ", {}, clear=True),                     mock.patch.object(session_notify, "_user_environment_value", return_value="fromreg"):
                self.assertEqual(session_notify._email_password({"password_env": "X_PW"}), "fromreg")

    def test_no_password_means_no_login_attempt(self):
        with mock.patch.object(session_notify, "_email_password", return_value=None),                 mock.patch("smtplib.SMTP") as smtp:
            ok = session_notify.send_email({"smtp_host": "h", "from": "a@b", "to": ["c@d"]}, "s", "b")
        self.assertFalse(ok)
        smtp.assert_not_called()

    def test_example_config_parses_and_names_no_secret(self):
        data = json.loads((ROOT / "notify_config.example.json").read_text(encoding="utf-8"))
        self.assertIn("email", data)
        self.assertNotIn("password", data["email"])
        self.assertIn("password_env", data["email"])


class ParticleRecordTests(unittest.TestCase):
    def test_sidecar_then_merge_after_the_session(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            # before the session: pending sidecar
            code = particle_record.main(["--output-dir", str(root), "--particle", "0", "--diameters", "1.5,1.6,1.5",
                                         "--calipers", "test 0.01 mm", "--placed-at", "2026-10-16T09:00:00"])
            self.assertEqual(code, 0)
            pending = json.loads((root / "particles_pending.json").read_text(encoding="utf-8"))
            self.assertEqual(pending[0]["diameters"][0]["mean_mm"], 1.533)
            self.assertEqual(pending[0]["diameters"][0]["spread_mm"], 0.1)
            # the session is running: no edit of the session JSON
            session = root / "auto_recording_session_20261016_090000.json"
            session.write_text(json.dumps({"status": "running", "runs": []}), encoding="utf-8")
            code = particle_record.main(["--session", str(session), "--particle", "0", "--dropped-run", "heart_s7_f10_OFF", "--merge"])
            self.assertEqual(code, 0)
            self.assertNotIn("particles", json.loads(session.read_text(encoding="utf-8")))
            sidecar = json.loads((root / "particles_20261016_090000.json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar[0]["dropped_runs"], ["heart_s7_f10_OFF"])
            self.assertTrue((root / "particles_pending.adopted.json").is_file())
            # after the session: removal time merges
            session.write_text(json.dumps({"status": "complete", "runs": []}), encoding="utf-8")
            code = particle_record.main(["--session", str(session), "--particle", "0", "--removed-at", "2026-10-16T12:00:00"])
            self.assertEqual(code, 0)
            merged = json.loads(session.read_text(encoding="utf-8"))["particles"]
            self.assertEqual(merged[0]["placed_at"], "2026-10-16T09:00:00")
            self.assertEqual(merged[0]["removed_at"], "2026-10-16T12:00:00")
            # a second particle is a second entry
            particle_record.main(["--session", str(session), "--particle", "1", "--diameters", "1.4,1.4,1.5", "--merge"])
            merged = json.loads(session.read_text(encoding="utf-8"))["particles"]
            self.assertEqual([p["particle"] for p in merged], ["0", "1"])

    def test_bad_diameters(self):
        with TemporaryDirectory() as temporary:
            self.assertEqual(particle_record.main(["--output-dir", temporary, "--particle", "0", "--diameters", "1.5,1.6"]), 2)
            self.assertEqual(particle_record.main(["--output-dir", temporary, "--particle", "0", "--placed-at", "yesterday"]), 2)


class ParticlePromptTests(unittest.TestCase):
    def test_parse(self):
        parse = particle_prompt.parse_particle_line
        self.assertIsNone(parse("", "0"))
        self.assertEqual(parse("1.5 1.6 1.5", "0"), {"particle": "0", "diameters_mm": [1.5, 1.6, 1.5], "note": ""})
        self.assertEqual(parse("1 1.5 1.6 1.5 chip", "0")["particle"], "1")
        self.assertEqual(parse("0' 1,4 1,5 1,5", "0"), {"particle": "0'", "diameters_mm": [1.4, 1.5, 1.5], "note": ""})
        self.assertEqual(parse("2", None), {"particle": "2", "diameters_mm": [], "note": ""})
        self.assertEqual(parse("1.5 1.6 1.5 ok", None, want_label=False)["diameters_mm"], [1.5, 1.6, 1.5])
        for bad in ("1.5 1.6", "0 1.5 1.6 25", "1.5 1.6 1.5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse(bad, None if bad == "1.5 1.6 1.5" else "0")

    def test_session_asks_at_start_change_and_after_collection(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                    "--hf-run", "kx=1", "--hf-run", "ky=1", "--hf-run", "kz=1",
                    "--unattended-after-first-checkpoint", "--particle-change-run-numbers", "3",
                    "--acknowledge-step-response-risk", "--no-notify",
                    "--particle-prompts", "--initial-particle", "0", "--particle-change-labels", "3:1",
                    "--particle-calipers", "test 0.01 mm", "--particle-measured-by", "op"]
            answers = iter(["1.5 1.6 1.5 clean",        # start: particle 0
                            "1.5 1.5 1.5",              # change: particle 0 after use
                            "1.4 1.4 1.5",              # change: new particle 1
                            "1.4 1.4 1.4"])             # end: particle 1 after collection
            reader = mock.Mock(side_effect=lambda *_a, **_k: next(answers))
            hold = mock.Mock(return_value={"ended": "enter", "held_sec": 2.0})
            with mock.patch("thermal_prompt.read_line_with_deadline", reader),                     mock.patch("builtins.input", return_value=""),                     mock.patch.object(auto_record, "hold_particle_for_collection", hold),                     mock.patch.object(auto_record, "_session_can_hold", return_value=True):
                result, _events, _ = run_main(argv)
            self.assertEqual(result, 0)
            self.assertEqual(reader.call_count, 4)
            session = json.loads(next((root / "rec").glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
            p0, p1 = session["particles"]
            self.assertEqual((p0["particle"], p1["particle"]), ("0", "1"))
            self.assertEqual([d["measured_when"] for d in p0["diameters"]], ["before", "after"])
            self.assertEqual(p0["diameters"][0]["mean_mm"], 1.533)
            self.assertEqual([r["label"] for r in p0["runs"]], ["kx", "ky"])
            self.assertEqual([r["label"] for r in p1["runs"]], ["kz"])
            self.assertEqual(p0["calipers"], "test 0.01 mm")
            self.assertEqual(p0["note"], "clean")
            self.assertTrue(p0["placed_at"] and p0["removed_at"] and p1["placed_at"] and p1["removed_at"])
            self.assertEqual(p1["removal"], "collected during the end-of-session hold")
            self.assertEqual([d["measured_when"] for d in p1["diameters"]], ["before", "after"])
            self.assertEqual([r.get("particle") for r in session["runs"]], ["0", "0", "1"])

    def test_no_answer_still_tracks_runs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_dir = write_export(root)
            argv = ["--hf-export-dir", str(export_dir), "--output-dir", str(root / "rec"),
                    "--hf-run", "kx=1", "--unattended-after-first-checkpoint", "--acknowledge-step-response-risk",
                    "--no-notify", "--no-hold-particle-until-enter", "--particle-prompts", "--initial-particle", "0"]
            with mock.patch("thermal_prompt.read_line_with_deadline", return_value=None):
                result, _events, _ = run_main(argv)
            self.assertEqual(result, 0)
            session = json.loads(next((root / "rec").glob("auto_recording_session_*.json")).read_text(encoding="utf-8"))
            (p0,) = session["particles"]
            self.assertEqual(p0["particle"], "0")
            self.assertEqual(p0["diameters"], [])
            self.assertEqual(p0["last_run"], "kx")
            self.assertIn("fell", p0["removal"])

    def test_particle_record_folds_into_typed_particles(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "auto_recording_session_20261016_090000.json"
            typed = [{"particle": "0", "diameters": [{"values_mm": [1.5, 1.6, 1.5], "measured_at": "t"}], "runs": [{"run_number": 1}]}]
            session.write_text(json.dumps({"status": "complete", "particles": typed}), encoding="utf-8")
            particle_record.main(["--session", str(session), "--particle", "0", "--source", "bag A", "--dropped-run", "kx", "--merge"])
            particle_record.main(["--session", str(session), "--particle", "0", "--merge"])   # re-merge: no duplicates
            (p0,) = json.loads(session.read_text(encoding="utf-8"))["particles"]
            self.assertEqual(p0["source"], "bag A")
            self.assertEqual(p0["dropped_runs"], ["kx"])
            self.assertEqual(len(p0["diameters"]), 1)
            self.assertEqual(p0["runs"], [{"run_number": 1}])


class ThermalFromSensorsTests(unittest.TestCase):
    def make(self, root: Path, ask: bool, env_ok: bool = True):
        import thermal_prompt
        reader = mock.Mock(return_value="")
        notes = thermal_prompt.ThermalNotes(
            csv_path=root / "thermal.csv", plan=[(0.0, 10.0)], timeout_sec=180.0, sound_on_wall_sec=1000.0,
            env_log=root / "env.csv", pat_temp_log=root / "pat.csv", reader=reader,
            ask_when_sensors_cover=ask)
        env = {"sensor_T_C": 24.1, "sensor_RH_percent": 50.0, "sensor_pressure_hPa": 1010.0,
               "sensor_dew_point_C": 13.0, "sensor_age_s": 5.0} if env_ok else {"sensor_T_C": None, "sensor_RH_percent": None}
        pat = {"pat_sensor_top_C": 30.5, "pat_sensor_bottom_C": 31.0, "pat_sensor_age_s": 1.0, "pat_sensor_values": "top_pat=30.5"}
        notes.latest_sensor = lambda: dict(env)
        notes.latest_pat_sensor = lambda: dict(pat)
        return notes, reader

    def test_all_fields_from_sensors_are_stored_without_asking(self):
        with TemporaryDirectory() as temporary:
            notes, reader = self.make(Path(temporary), ask=False)
            record = notes.ask("scheduled", 5, "kcheck_x", None)
            reader.assert_not_called()
            self.assertEqual(record["source"], "sensors")
            self.assertEqual((record["pat_sensor_top_C"], record["sensor_T_C"]), (30.5, 24.1))
            self.assertIsNone(record["T_top_PAT_C"])
            self.assertTrue((Path(temporary) / "thermal.csv").is_file())

    def test_asks_when_a_sensor_is_missing_or_when_asked_to(self):
        with TemporaryDirectory() as temporary:
            notes, reader = self.make(Path(temporary), ask=False, env_ok=False)
            notes.ask("scheduled", 5, "kcheck_x", None)
            reader.assert_called_once()
        with TemporaryDirectory() as temporary:
            notes, reader = self.make(Path(temporary), ask=True)
            notes.ask("scheduled", 5, "kcheck_x", None)
            reader.assert_called_once()


if __name__ == "__main__":
    unittest.main()
