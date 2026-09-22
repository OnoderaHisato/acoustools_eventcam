"""Hardware-free tests for the Miyabi upload helper (no ssh, no network, no measurement data touched)."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

import miyabi_upload


def make_run(root: Path, name: str, files: dict[str, bytes]) -> Path:
    run = root / name
    for relative, payload in files.items():
        path = run / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return run


def sample_records(root: Path) -> Path:
    records = root / "stereo_acoustools_3d_records_test"
    make_run(
        records,
        "feedforward_validation_033_ffheart_s7_f7_CWxz_scale100_20260921_010000",
        {
            "pipeline_manifest.json": b'{"capture_complete": true}',
            # Nested paths are the Windows-specific risk: they must become "/" inside the tar.
            "stereo_recording/left/left_events.npz": b"L" * 2048,
            "stereo_recording/right/right_events.npz": b"R" * 1024,
            # macOS resource forks that may ride along on a shared drive.
            "stereo_recording/._left_events.npz": b"junk",
        },
    )
    make_run(
        records,
        "step_response_identification_000_kcheck_x_S105_6jumps_scale100_20260920_235900",
        {"pipeline_manifest.json": b"{}", "stereo_recording/left/left_events.npz": b"x" * 512},
    )
    return records


class FakeSsh:
    """Stands in for the ssh process: keeps the tar stream and answers like Miyabi would."""

    def __init__(self, sizes: dict[str, int], qsub: str = "3412345.opbs"):
        self.sizes = sizes
        self.qsub = qsub
        self.commands: list[list[str]] = []
        self.streams: list[bytes] = []
        self.returncode = 0

    def __call__(self, argv, stdin=None, stdout=None):
        self.commands.append(argv)
        buffer = io.BytesIO()
        outer = self

        class Process:
            def __init__(self) -> None:
                self.stdin = buffer
                self.returncode = 0

            @property
            def stdout(self):
                outer.streams.append(buffer.getvalue())
                lines = [f"SIZE {name} {size}" for name, size in outer.sizes.items()]
                lines.append(f"QSUB {outer.qsub}" if outer.qsub else "QSUB skipped")
                return io.BytesIO(("\n".join(lines) + "\n").encode())

            def wait(self):
                return 0

        # gzip writes to stdin and the code closes it; keep the bytes readable afterwards.
        buffer.close = lambda: None  # type: ignore[method-assign]
        return Process()


def tar_members(stream: bytes) -> dict[str, bytes]:
    with gzip.GzipFile(fileobj=io.BytesIO(stream)) as gz, tarfile.open(fileobj=gz, mode="r|") as tf:
        members = {}
        for info in tf:
            if info.isfile():
                members[info.name] = tf.extractfile(info).read()
        return members


class RunSelectionTests(unittest.TestCase):
    def test_stamp_and_local_bytes_ignore_resource_forks(self) -> None:
        with TemporaryDirectory() as temporary:
            records = sample_records(Path(temporary))
            run = records / "feedforward_validation_033_ffheart_s7_f7_CWxz_scale100_20260921_010000"
            self.assertEqual(miyabi_upload.stamp(run.name), "20260921_010000")
            self.assertEqual(miyabi_upload.stamp("no_timestamp_here"), "")
            # 2048 + 1024 + the manifest; the "._" file is not counted.
            self.assertEqual(miyabi_upload.local_bytes(run), 2048 + 1024 + len(b'{"capture_complete": true}'))

    def test_dry_run_selects_filters_and_puts_feedforward_first(self) -> None:
        with TemporaryDirectory() as temporary:
            records = sample_records(Path(temporary))
            with mock.patch.object(miyabi_upload.subprocess, "Popen") as popen:
                with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    miyabi_upload.main.__wrapped__() if hasattr(miyabi_upload.main, "__wrapped__") else None
                    with mock.patch("sys.argv", ["miyabi_upload.py", str(records), "--dry-run"]):
                        miyabi_upload.main()
                printed = out.getvalue()
            popen.assert_not_called()
            first = printed.index("feedforward_validation_033")
            second = printed.index("step_response_identification_000")
            self.assertLess(first, second)
            # --since and --match narrow the selection.
            for argv, expected in (
                (["--since", "20260921_000000"], ["feedforward_validation_033"]),
                (["--match", "kcheck"], ["step_response_identification_000"]),
            ):
                with self.subTest(argv=argv):
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                        with mock.patch("sys.argv", ["miyabi_upload.py", str(records)] + argv + ["--dry-run"]):
                            miyabi_upload.main()
                    printed = out.getvalue()
                    self.assertIn(expected[0], printed)
                    self.assertEqual(printed.count("_scale100_"), 1)

    def test_no_matching_run_exits(self) -> None:
        with TemporaryDirectory() as temporary:
            records = sample_records(Path(temporary))
            with mock.patch("sys.argv", ["miyabi_upload.py", str(records), "--match", "nothing"]):
                with self.assertRaises(SystemExit):
                    miyabi_upload.main()


class SendBatchTests(unittest.TestCase):
    def _send(self, records: Path, runs: list[Path], sizes: dict[str, int], **kwargs):
        fake = FakeSsh(sizes, qsub=kwargs.pop("qsub", "3412345.opbs"))
        with mock.patch.object(miyabi_upload.subprocess, "Popen", fake):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                ok = miyabi_upload.send_batch(records, runs, kwargs.pop("tag", "0921a"), kwargs.pop("no_qsub", False), 1)
        return ok, out.getvalue(), fake

    def test_stream_keeps_posix_paths_and_drops_resource_forks(self) -> None:
        with TemporaryDirectory() as temporary:
            records = sample_records(Path(temporary))
            runs = sorted(records.iterdir())
            sizes = {run.name: miyabi_upload.local_bytes(run) for run in runs}
            ok, printed, fake = self._send(records, runs, sizes)
            self.assertTrue(ok)
            members = tar_members(fake.streams[0])
            expected = (
                "feedforward_validation_033_ffheart_s7_f7_CWxz_scale100_20260921_010000/"
                "stereo_recording/left/left_events.npz"
            )
            self.assertIn(expected, members)
            self.assertEqual(members[expected], b"L" * 2048)
            self.assertFalse([name for name in members if Path(name).name.startswith("._")])
            self.assertFalse([name for name in members if "\\" in name])
            self.assertIn("OK      ", printed)
            self.assertIn("QSUB 3412345.opbs", printed)

    def test_remote_command_pins_group_list_and_job(self) -> None:
        with TemporaryDirectory() as temporary:
            records = sample_records(Path(temporary))
            runs = sorted(records.iterdir())
            sizes = {run.name: miyabi_upload.local_bytes(run) for run in runs}
            _ok, _printed, fake = self._send(records, runs, sizes, tag="0921a")
            argv = fake.commands[0]
            self.assertEqual(argv[0], "ssh")
            self.assertIn(miyabi_upload.HOST, argv)
            remote = argv[-1]
            self.assertIn(f"{miyabi_upload.WORK}/{records.name}", remote)
            self.assertIn(f"chgrp -R {miyabi_upload.GROUP}", remote)
            self.assertIn("chmod g+s", remote)
            self.assertIn("list_0921a.txt", remote)
            self.assertIn(f"qsub -q regular-c -N up_0921a", remote)
            self.assertIn(f"NPAR={len(runs)}", remote)
            self.assertIn(miyabi_upload.PBS, remote)

    def test_no_qsub_sends_without_submitting(self) -> None:
        with TemporaryDirectory() as temporary:
            records = sample_records(Path(temporary))
            runs = sorted(records.iterdir())
            sizes = {run.name: miyabi_upload.local_bytes(run) for run in runs}
            _ok, printed, fake = self._send(records, runs, sizes, no_qsub=True, qsub="")
            self.assertNotIn("qsub -q", fake.commands[0][-1])
            self.assertIn("QSUB skipped", printed)

    def test_size_mismatch_is_reported(self) -> None:
        with TemporaryDirectory() as temporary:
            records = sample_records(Path(temporary))
            runs = sorted(records.iterdir())
            sizes = {run.name: miyabi_upload.local_bytes(run) for run in runs}
            broken = runs[0].name
            sizes[broken] -= 1
            ok, printed, _fake = self._send(records, runs, sizes)
            self.assertFalse(ok)
            self.assertIn("MISMATCH", printed)
            self.assertIn(broken, printed)

    def test_batches_are_capped_at_nine_runs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            for index in range(11):
                make_run(records, f"step_response_identification_{index:03d}_r_scale100_20260921_0100{index:02d}", {"a.bin": b"a"})
            runs = sorted(records.iterdir())
            sizes = {run.name: 1 for run in runs}
            fake = FakeSsh(sizes)
            with mock.patch.object(miyabi_upload.subprocess, "Popen", fake):
                with mock.patch("sys.stdout", new_callable=io.StringIO):
                    with mock.patch("sys.argv", ["miyabi_upload.py", str(records)]):
                        miyabi_upload.main()
            self.assertEqual(len(fake.commands), 2)
            first_batch, second_batch = (argv[-1] for argv in fake.commands)
            for run in runs[:9]:
                self.assertIn(run.name, first_batch)
                self.assertNotIn(run.name, second_batch)
            for run in runs[9:]:
                self.assertIn(run.name, second_batch)
                self.assertNotIn(run.name, first_batch)
            # The batch tag is MMDDHHMM of the first run plus a, b, ... per batch.
            tags = [argv[-1] for argv in fake.commands]
            self.assertIn("list_09210100a.txt", tags[0])
            self.assertIn("list_09210100b.txt", tags[1])


if __name__ == "__main__":
    unittest.main()
