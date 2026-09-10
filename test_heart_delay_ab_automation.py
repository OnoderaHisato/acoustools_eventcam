import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import acoustools_stereo_heart_delay_ab as heart_ab


class HeartDelayAbAutomationTests(unittest.TestCase):
    def test_heart_capture_defaults_to_nominal_plus_point_three_seconds(self) -> None:
        args = heart_ab.parse_args([])
        self.assertEqual(args.post_roll_sec, 0.3)
        self.assertEqual(args.capture_tail_margin_sec, 0.0)
        self.assertFalse(args.postprocess_and_compare)

    def test_automatic_analysis_runs_postprocess_then_compare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session = root / "session.json"
            runs = [root / "baseline", root / "delay"]
            with patch.object(subprocess, "run") as run:
                heart_ab.run_automatic_analysis(session, runs)

        self.assertEqual(run.call_count, 2)
        postprocess_command = run.call_args_list[0].args[0]
        compare_command = run.call_args_list[1].args[0]
        self.assertIn("stereo_acoustools_3d_postprocess.py", postprocess_command[1])
        self.assertEqual(postprocess_command[2:4], [str(runs[0]), str(runs[1])])
        self.assertIn("--resume", postprocess_command)
        self.assertIn("--keep-going", postprocess_command)
        self.assertIn("compare_heart_delay_ab.py", compare_command[1])
        self.assertEqual(compare_command[2], str(session))
        self.assertTrue(all(call.kwargs.get("check") for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
