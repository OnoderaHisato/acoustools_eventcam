"""Convert a Metavision/SilkyEvCam RAW recording into reviewable files.

RAW ファイルは通常の動画ではなく、各イベントの x, y, p, t を持つイベント列です。
このスクリプトは RAW または撮影 summary JSON を読み、イベント列から確認用 MP4、
必要なら PNG 連番、各フレームのタイムスタンプ CSV を作ります。

実行例:
    python eventcam_raw_postprocess.py recordings_eventcam/eventcam_enter_20260527_204459_001.json
    python eventcam_raw_postprocess.py recordings_eventcam/eventcam_enter_20260527_204459_001.raw --fps 50
    python eventcam_raw_postprocess.py recordings_eventcam/eventcam_enter_20260527_204459_001.json --fps 1000 --video-fps 50
    python eventcam_raw_postprocess.py recordings_eventcam/eventcam_enter_20260527_204459_001.json --save-png --max-frames 100
"""

# 型ヒントの評価を遅らせ、実行時の import 負荷を少し下げます。
from __future__ import annotations

# コマンドライン引数を扱うための標準ライブラリです。
import argparse

# フレームごとのタイムスタンプやイベント数を CSV に保存するために使います。
import csv

# summary JSON から RAW パスを読むために使います。
import json

# OS に依存しにくい形でファイルパスを扱うために使います。
from pathlib import Path

# 画像配列を作るために使います。
import numpy as np

# 動画や PNG 画像を書き出すために使います。
import cv2

# OpenEB DLL / plugin 探索パス設定をこのスクリプト内で行います。
import os


_DLL_DIRECTORY_HANDLES = []
_DLL_DIRECTORY_PATHS: set[str] = set()


def prepend_environment_paths(variable_name: str, directories: list[Path]) -> None:
    existing_parts = [part for part in os.environ.get(variable_name, "").split(os.pathsep) if part]
    seen = {os.path.normcase(os.path.abspath(part)) for part in existing_parts}
    new_parts: list[str] = []
    for directory in directories:
        if not directory.exists():
            continue
        resolved = str(directory.resolve())
        key = os.path.normcase(os.path.abspath(resolved))
        if key in seen:
            continue
        new_parts.append(resolved)
        seen.add(key)
    if new_parts:
        os.environ[variable_name] = os.pathsep.join(new_parts + existing_parts)


def configure_local_metavision_environment() -> None:
    project_dir = Path(__file__).resolve().parent
    openeb_install_dir = project_dir / "openeb_install"
    openeb_bin_dir = openeb_install_dir / "bin"
    vcpkg_bin_dir = project_dir / "vcpkg" / "vcpkg_installed" / "x64-windows" / "bin"
    if not vcpkg_bin_dir.exists():
        vcpkg_bin_dir = project_dir / "vcpkg" / "installed" / "x64-windows" / "bin"
    hal_plugin_dir = openeb_install_dir / "lib" / "metavision" / "hal" / "plugins"
    hdf5_plugin_dir = openeb_install_dir / "lib" / "hdf5" / "plugin"
    centuryarks_dir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "CenturyArks"
    centuryarks_bin_dir = centuryarks_dir / "bin"
    centuryarks_plugin_dir = centuryarks_dir / "plugins"

    dll_dirs = [openeb_bin_dir, vcpkg_bin_dir, hal_plugin_dir, centuryarks_bin_dir, centuryarks_plugin_dir]
    prepend_environment_paths("PATH", dll_dirs)
    prepend_environment_paths("MV_HAL_PLUGIN_PATH", [hal_plugin_dir, centuryarks_plugin_dir])
    prepend_environment_paths("HDF5_PLUGIN_PATH", [hdf5_plugin_dir])

    if hasattr(os, "add_dll_directory"):
        for directory in dll_dirs:
            if not directory.exists():
                continue
            resolved = str(directory.resolve())
            if resolved in _DLL_DIRECTORY_PATHS:
                continue
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(resolved))
            _DLL_DIRECTORY_PATHS.add(resolved)


# コマンドライン引数を定義して読み取る関数です。
def parse_args() -> argparse.Namespace:
    # デフォルト値も help に出る argparse parser を作ります。
    parser = argparse.ArgumentParser(
        description="Render an event-camera RAW file into MP4/PNG frames and frame timestamp CSV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # 入力ファイルです。RAW そのものでも、撮影時に出した JSON でも受け付けます。
    parser.add_argument("input", help="Input .raw file or capture summary .json file.")

    # 出力先の親ディレクトリです。RAW 名のサブフォルダをこの中に作ります。
    parser.add_argument("--output-dir", default="processed_eventcam", help="Parent directory for converted outputs.")

    # 何 fps 相当でイベントをフレーム化するかを指定します。
    parser.add_argument("--fps", type=float, default=25.0, help="Render rate used to slice events into frames.")

    # MP4 の再生 fps です。未指定なら --fps と同じ値にします。
    parser.add_argument("--video-fps", type=float, default=None, help="Playback frame rate written into the MP4. Empty uses --fps.")

    # 1 枚の画像へ積算するイベント時間幅です。0 なら各フレーム区間内の全イベントを使います。
    parser.add_argument("--accumulation-us", type=int, default=0, help="Event accumulation time per frame in microseconds.")

    # 動作確認や短いプレビュー用に、処理するフレーム数の上限を指定できます。
    parser.add_argument("--max-frames", type=int, default=0, help="Maximum number of frames to render. 0 means all.")

    # 指定すると PNG 連番も保存します。長時間 RAW ではファイル数が多くなるのでオプションにしています。
    parser.add_argument("--save-png", action="store_true", help="Also save rendered frames as PNG images.")

    # 指定すると MP4 を作らず、CSV と必要なら PNG だけを書き出します。
    parser.add_argument("--no-video", action="store_true", help="Do not write MP4 video.")

    # LED同期マーカーを探すROIです。指定すると、RAWイベントから開始時刻を自動検出します。
    parser.add_argument("--sync-led-roi", default="", help="LED sync ROI as x0,y0,x1,y1. Example: 0,0,320,180.")

    # LED同期検出時に使う時間bin幅です。
    parser.add_argument("--sync-led-bin-us", type=int, default=100, help="Time bin used for LED sync detection in microseconds.")

    # LED同期検出の閾値です。0ならROIイベント数から自動推定します。
    parser.add_argument("--sync-led-threshold", type=int, default=0, help="LED ROI event-count threshold per detection bin. 0 means auto.")

    # LED検出後、出力開始を何us前に戻すかです。
    parser.add_argument("--sync-pre-roll-us", type=int, default=0, help="Keep this many microseconds before detected LED onset.")

    # LED検出後、処理する長さを制限します。0なら最後まで処理します。
    parser.add_argument("--duration-us", type=int, default=0, help="Output duration after sync start in microseconds. 0 means until RAW end.")

    # 指定すると、LED ROIを出力イベントから除外します。
    parser.add_argument("--mask-led-roi", action="store_true", help="Remove events inside --sync-led-roi from rendered output and CSV stats.")

    # 任意の追加マスクROIです。複数指定できます。
    parser.add_argument("--mask-roi", action="append", default=[], help="Remove events inside ROI x0,y0,x1,y1. Can be specified multiple times.")

    # 指定すると、同期切り抜き・マスク後のイベント列を npz として保存します。
    parser.add_argument("--export-filtered-events-npz", action="store_true", help="Export filtered events as compressed NPZ for trajectory analysis.")

    # 実際に指定された引数を Namespace として返します。
    return parser.parse_args()


# 入力が JSON の場合は raw_path を取り出し、RAW の場合はそのまま返す関数です。
def resolve_raw_path(input_path: Path) -> Path:
    # 拡張子を小文字で見て、JSON かどうか判断します。
    if input_path.suffix.lower() != ".json":
        # JSON でなければ RAW とみなして、そのまま絶対パス化します。
        return input_path.resolve()

    # summary JSON を UTF-8 として読み込みます。
    summary = json.loads(input_path.read_text(encoding="utf-8"))

    # 撮影スクリプトが保存した raw_path を取り出します。
    raw_path = summary.get("raw_path")

    # raw_path がない JSON は、この後処理には使えないので止めます。
    if not raw_path:
        raise SystemExit(f"{input_path} does not contain raw_path.")

    # JSON 内の raw_path を Path にして返します。
    return Path(raw_path).resolve()


def load_summary_payload(input_path: Path) -> dict:
    if input_path.suffix.lower() != ".json":
        return {}
    return json.loads(input_path.read_text(encoding="utf-8"))


def postprocess_output_dir_for_raw(output_root: str, raw_path: Path, payload: dict) -> Path:
    root = Path(output_root)
    extra_meta = payload.get("extra_meta", {}) if isinstance(payload, dict) else {}
    if isinstance(extra_meta, dict):
        shape_name = str(extra_meta.get("shape_name") or "").strip()
        run_dir = str(extra_meta.get("run_dir") or "").strip()
        if shape_name and run_dir:
            return root / shape_name / Path(run_dir).name

    for recording_root in [Path("rec_eventcam"), Path("recordings_eventcam_acoustools"), Path("recordings_eventcam")]:
        try:
            rel_parent = raw_path.parent.resolve().relative_to(recording_root.resolve())
            return root / rel_parent
        except Exception:
            continue

    return root / raw_path.stem


# Metavision SDK の必要部分を読み込む関数です。
def import_metavision() -> tuple[object, object]:
    # venv の python.exe 直指定でも OpenEB の DLL / plugin が見えるようにします。
    configure_local_metavision_environment()

    # RAW を時間区間ごとに読み出す iterator を読み込みます。
    from metavision_core.event_io import EventsIterator

    # イベント列を確認用 BGR 画像へ描画するアルゴリズムを読み込みます。
    from metavision_sdk_core import PeriodicFrameGenerationAlgorithm, ColorPalette

    # 呼び出し側で使う API を返します。
    return EventsIterator, (PeriodicFrameGenerationAlgorithm, ColorPalette)


# ROI 文字列を x0, y0, x1, y1 の整数タプルに変換する関数です。
def parse_roi(text: str, name: str) -> tuple[int, int, int, int]:
    # カンマ区切りの4要素を想定します。
    parts = [part.strip() for part in text.split(",") if part.strip()]

    # 要素数が違う場合は分かりやすく止めます。
    if len(parts) != 4:
        raise SystemExit(f"{name} must be x0,y0,x1,y1: {text}")

    # 整数へ変換します。
    try:
        x0, y0, x1, y1 = [int(part) for part in parts]
    except ValueError as exc:
        raise SystemExit(f"{name} must contain integers: {text}") from exc

    # 右下が左上より大きいROIだけを受け付けます。
    if x1 <= x0 or y1 <= y0:
        raise SystemExit(f"{name} must satisfy x1>x0 and y1>y0: {text}")

    # ROI を返します。
    return x0, y0, x1, y1


# ROI 内のイベントだけを選ぶ boolean mask を作る関数です。
def make_roi_mask(events: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    # ROI 境界を取り出します。
    x0, y0, x1, y1 = roi

    # x/y が ROI 内にあるイベントを True にします。
    return (events["x"] >= x0) & (events["x"] < x1) & (events["y"] >= y0) & (events["y"] < y1)


# 複数ROI内のイベントを除外する関数です。
def remove_masked_events(events: np.ndarray, rois: list[tuple[int, int, int, int]]) -> np.ndarray:
    # ROI がなければそのまま返します。
    if not rois or events.size == 0:
        return events

    # 最初は全イベントを残す設定にします。
    keep = np.ones(events.shape[0], dtype=bool)

    # 各 ROI 内のイベントを落とします。
    for roi in rois:
        keep &= ~make_roi_mask(events, roi)

    # 残ったイベントだけを返します。
    return events[keep]


# 左上LEDなどの同期マーカーをRAWイベントから検出する関数です。
def detect_led_sync(
    EventsIterator: object,
    raw_path: Path,
    roi: tuple[int, int, int, int],
    bin_us: int,
    threshold: int,
    output_dir: Path,
) -> dict[str, object]:
    # bin 幅が不正なら止めます。
    if bin_us <= 0:
        raise SystemExit("--sync-led-bin-us must be positive.")

    # 検出用に短い delta_t で RAW を読みます。
    iterator = EventsIterator(
        input_path=str(raw_path),
        mode="delta_t",
        delta_t=bin_us,
        relative_timestamps=False,
    )

    # 検出結果をメモリ上に貯めます。通常は数万行程度なので十分軽いです。
    rows: list[dict[str, int]] = []

    # 各binのROI内イベント数を数えます。
    for bin_index, events in enumerate(iterator):
        # このbinの推定開始時刻です。
        bin_start_ts_us = int(bin_index * bin_us)

        # ROI 内イベント数と、その最初/最後の実イベント時刻を取ります。
        if events.size == 0:
            roi_count = 0
            roi_first_ts_us = -1
            roi_last_ts_us = -1
        else:
            roi_events = events[make_roi_mask(events, roi)]
            roi_count = int(roi_events.size)
            if roi_count:
                roi_first_ts_us = int(roi_events["t"][0])
                roi_last_ts_us = int(roi_events["t"][-1])
            else:
                roi_first_ts_us = -1
                roi_last_ts_us = -1

        # CSV 用の行を保存します。
        rows.append(
            {
                "bin_index": int(bin_index),
                "bin_start_ts_us": bin_start_ts_us,
                "roi_count": roi_count,
                "roi_first_ts_us": roi_first_ts_us,
                "roi_last_ts_us": roi_last_ts_us,
            }
        )

    # イベントが読めないRAWなら止めます。
    if not rows:
        raise SystemExit("No event slices were read while detecting LED sync.")

    # ROI count 配列を作ります。
    counts = np.array([row["roi_count"] for row in rows], dtype=np.int64)

    # 閾値が 0 なら、中央値 + MAD ベースで自動推定します。
    if threshold <= 0:
        median = float(np.median(counts))
        mad = float(np.median(np.abs(counts - median)))
        threshold = max(1, int(np.ceil(median + max(5.0, 8.0 * mad))))

    peak_index = int(np.argmax(counts))

    # 閾値以上のbinを探します。
    hit_indices = np.flatnonzero(counts >= threshold)
    if hit_indices.size == 0:
        peak_count = int(counts[peak_index])
        raise SystemExit(
            "LED sync was not detected. "
            f"max_count={peak_count}, threshold={threshold}, "
            f"peak_bin={peak_index}, peak_time_us={rows[peak_index]['bin_start_ts_us']}. "
            "Try a larger ROI or lower --sync-led-threshold."
        )

    # LEDは一定時間点灯するため、RAW全体で最初に閾値を超えたbinではなく、
    # 最大ピークを含む連続した閾値超え区間の先頭を同期時刻にします。
    # これにより、ROI内の早い孤立イベントを開始マーカーと誤認しにくくします。
    sync_bin_index = int(peak_index)
    while sync_bin_index > 0 and int(counts[sync_bin_index - 1]) >= int(threshold):
        sync_bin_index -= 1
    sync_row = rows[sync_bin_index]

    # ROIイベントがある場合は、その最初のイベント時刻を同期時刻にします。
    if sync_row["roi_first_ts_us"] >= 0:
        sync_ts_us = int(sync_row["roi_first_ts_us"])
    else:
        sync_ts_us = int(sync_row["bin_start_ts_us"])

    # ピーク情報も保存します。
    peak_row = rows[peak_index]

    # 検出CSVを書き出します。
    report_path = output_dir / f"{raw_path.stem}_led_sync_detection.csv"
    with report_path.open("w", newline="", encoding="utf-8") as csv_file:
        fieldnames = ["bin_index", "bin_start_ts_us", "roi_count", "roi_first_ts_us", "roi_last_ts_us"]
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # 検出結果を返します。
    return {
        "sync_ts_us": sync_ts_us,
        "sync_bin_index": sync_bin_index,
        "sync_bin_start_ts_us": int(sync_row["bin_start_ts_us"]),
        "threshold": int(threshold),
        "peak_bin_index": peak_index,
        "peak_bin_start_ts_us": int(peak_row["bin_start_ts_us"]),
        "peak_count": int(peak_row["roi_count"]),
        "report_path": str(report_path.resolve()),
    }


# 1 フレーム分のイベント統計を CSV 用の辞書に変換する関数です。
def make_frame_row(
    frame_index: int,
    frame_ts_us: int,
    events: np.ndarray,
    image_path: Path | None,
    source_frame_ts_us: int | None = None,
    sync_ts_us: int | None = None,
) -> dict[str, object]:
    # このフレーム区間のイベント数を数えます。
    event_count = int(events.size)

    # 空フレームではイベント時刻がないため、フレーム基準時刻を start/end として使います。
    if event_count == 0:
        # 空フレームの開始時刻です。
        start_ts_us = frame_ts_us

        # 空フレームの終了時刻です。
        end_ts_us = frame_ts_us

        # ON イベント数は 0 です。
        on_count = 0

        # OFF イベント数も 0 です。
        off_count = 0

    # イベントがあるフレームでは、実際に入っているイベント時刻から start/end を取ります。
    else:
        # このフレームに含まれる最初のイベント時刻です。
        start_ts_us = int(events["t"][0])

        # このフレームに含まれる最後のイベント時刻です。
        end_ts_us = int(events["t"][-1])

        # p が 0 でないイベントを ON として数えます。
        on_count = int(np.count_nonzero(events["p"]))

        # 全体から ON を引いた数を OFF として数えます。
        off_count = event_count - on_count

    # CSV へ 1 行として書く辞書を返します。
    return {
        "frame_index": frame_index,
        "frame_ts_us": frame_ts_us,
        "source_frame_ts_us": "" if source_frame_ts_us is None else source_frame_ts_us,
        "sync_ts_us": "" if sync_ts_us is None else sync_ts_us,
        "slice_start_ts_us": start_ts_us,
        "slice_end_ts_us": end_ts_us,
        "event_count": event_count,
        "on_count": on_count,
        "off_count": off_count,
        "image_path": "" if image_path is None else str(image_path.resolve()),
    }


# RAW を読み、MP4/PNG/CSV を書き出すメイン処理です。
def main() -> None:
    # コマンドライン引数を読み取ります。
    args = parse_args()

    # fps が 0 以下だとフレーム区間を決められないので止めます。
    if args.fps <= 0:
        raise SystemExit("--fps must be positive.")

    # video_fps 未指定なら、従来通り render fps と同じ値で動画を書きます。
    video_fps = float(args.fps if args.video_fps is None else args.video_fps)

    # video_fps が 0 以下だと動画ヘッダを書けないので止めます。
    if video_fps <= 0:
        raise SystemExit("--video-fps must be positive.")

    # accumulation が負だと意味がないので止めます。
    if args.accumulation_us < 0:
        raise SystemExit("--accumulation-us must be zero or positive.")

    # 入力パスを Path にします。
    input_path = Path(args.input)

    # JSON 入力なら raw_path を読み、RAW 入力ならそのまま使います。
    raw_path = resolve_raw_path(input_path)
    summary_payload = load_summary_payload(input_path)

    # RAW ファイルが存在するか確認します。
    if not raw_path.exists():
        raise SystemExit(f"RAW file not found: {raw_path}")

    # Metavision の読み出し API と描画 API を読み込みます。
    EventsIterator, core_api = import_metavision()

    # tuple から描画アルゴリズムとカラーパレットを取り出します。
    PeriodicFrameGenerationAlgorithm, ColorPalette = core_api

    # fps から 1 フレームに対応する時間幅をマイクロ秒で計算します。
    frame_period_us = int(round(1_000_000 / args.fps))

    # rec_eventcam と同じ shape/run_dir 階層で proc_eventcam 側にも出力します。
    output_dir = postprocess_output_dir_for_raw(args.output_dir, raw_path, summary_payload)

    # 出力フォルダがなければ作ります。
    output_dir.mkdir(parents=True, exist_ok=True)

    # LED同期ROIを指定された場合は、RAWイベントから同期時刻を検出します。
    led_roi = parse_roi(args.sync_led_roi, "--sync-led-roi") if args.sync_led_roi else None
    sync_result: dict[str, object] | None = None
    sync_ts_us: int | None = None
    if led_roi is not None:
        sync_result = detect_led_sync(
            EventsIterator=EventsIterator,
            raw_path=raw_path,
            roi=led_roi,
            bin_us=int(args.sync_led_bin_us),
            threshold=int(args.sync_led_threshold),
            output_dir=output_dir,
        )
        sync_ts_us = int(sync_result["sync_ts_us"])
        print(
            "LED sync detected: "
            f"sync_ts_us={sync_ts_us}, "
            f"threshold={sync_result['threshold']}, "
            f"peak_count={sync_result['peak_count']}, "
            f"report={sync_result['report_path']}"
        )

    # 出力開始・終了時刻を決めます。sync がない場合は RAW 先頭から処理します。
    output_start_ts_us = 0
    if sync_ts_us is not None:
        output_start_ts_us = max(0, int(sync_ts_us) - int(args.sync_pre_roll_us))

    # duration が指定されていれば出力終了時刻を決めます。
    output_end_ts_us = None
    if args.duration_us > 0:
        output_end_ts_us = output_start_ts_us + int(args.duration_us)

    # EventsIterator の start_ts は delta_t の倍数である必要があるため、直前の境界へ丸めます。
    iterator_start_ts_us = output_start_ts_us - (output_start_ts_us % frame_period_us)

    # 指定durationがある場合だけ max_duration を設定します。
    iterator_max_duration = None
    if output_end_ts_us is not None:
        iterator_max_duration = max(frame_period_us, output_end_ts_us - iterator_start_ts_us)

    # RAW を frame_period_us ごとのイベント配列として読む iterator を作ります。
    iterator = EventsIterator(
        input_path=str(raw_path),
        start_ts=iterator_start_ts_us,
        mode="delta_t",
        delta_t=frame_period_us,
        max_duration=iterator_max_duration,
        relative_timestamps=False,
    )

    # センサー解像度を取得します。戻り値は height, width の順です。
    height, width = iterator.get_size()

    # マスクROIを集めます。
    mask_rois = [parse_roi(text, "--mask-roi") for text in args.mask_roi]
    if args.mask_led_roi:
        if led_roi is None:
            raise SystemExit("--mask-led-roi requires --sync-led-roi.")
        mask_rois.append(led_roi)

    # PNG 連番を保存する場合のサブフォルダを準備します。
    frames_dir = output_dir / "frames"

    # PNG 保存が有効なら frames フォルダを作ります。
    if args.save_png:
        frames_dir.mkdir(parents=True, exist_ok=True)

    # MP4 出力ファイルのパスです。render fps と playback fps が違う場合は両方を名前に入れます。
    accumulation_tag = "" if int(args.accumulation_us) == 0 else f"_acc{int(args.accumulation_us)}us"
    sync_tag = "_syncled" if led_roi is not None else ""
    mask_tag = "_masked" if mask_rois else ""
    video_path = output_dir / f"{raw_path.stem}_render.mp4"

    # CSV 出力ファイルのパスです。CSV の時刻は render fps 側の 1 フレーム区間に対応します。
    # Keep this short enough for Windows paths when trajectory stems are long.
    csv_path = output_dir / f"{raw_path.stem}_timestamps.csv"

    # 同期情報をJSONにも保存します。
    sync_json_path = None
    if sync_result is not None:
        sync_json_path = output_dir / f"{raw_path.stem}_led_sync.json"
        sync_payload = {
            "raw_path": str(raw_path.resolve()),
            "led_roi": list(led_roi) if led_roi is not None else None,
            "mask_rois": [list(roi) for roi in mask_rois],
            "sync_result": sync_result,
            "output_start_ts_us": output_start_ts_us,
            "output_end_ts_us": output_end_ts_us,
            "sync_pre_roll_us": int(args.sync_pre_roll_us),
            "duration_us": int(args.duration_us),
        }
        sync_json_path.write_text(json.dumps(sync_payload, indent=2), encoding="utf-8")

    # フィルタ後イベントを書き出す場合の保存先と一時バッファです。
    filtered_events_path = None
    filtered_event_chunks: list[np.ndarray] = []
    if args.export_filtered_events_npz:
        filtered_events_path = output_dir / f"{raw_path.stem}{sync_tag}{mask_tag}_events.npz"

    # 動画 writer は必要な場合だけ作ります。
    video_writer = None

    # --no-video が指定されていなければ MP4 writer を初期化します。
    if not args.no_video:
        # mp4v は OpenCV で扱いやすい MPEG-4 系 codec です。
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")

        # OpenCV の VideoWriter を開きます。
        video_writer = cv2.VideoWriter(str(video_path), fourcc, video_fps, (int(width), int(height)))

        # writer が開けなかった場合は、動画保存なしで続けるより明示的に止めます。
        if not video_writer.isOpened():
            raise SystemExit(f"Could not open video writer: {video_path}")

    # CSV ファイルを開き、フレームごとの時刻とイベント数を書き出します。
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        # CSV の列名を定義します。
        fieldnames = [
            "frame_index",
            "frame_ts_us",
            "source_frame_ts_us",
            "sync_ts_us",
            "slice_start_ts_us",
            "slice_end_ts_us",
            "event_count",
            "on_count",
            "off_count",
            "image_path",
        ]

        # 辞書を CSV 行へ変換する writer を作ります。
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)

        # ヘッダー行を書き込みます。
        writer.writeheader()

        # 出力フレーム番号です。同期前のスライスを捨てる場合があるため iterator の番号とは分けます。
        output_frame_index = 0

        # iterator からフレーム区間ごとのイベント配列を順番に読みます。
        for source_frame_index, events in enumerate(iterator):
            # max_frames が指定されている場合、上限に達したら処理を終えます。
            if args.max_frames and output_frame_index >= args.max_frames:
                break

            # この入力スライスのRAW内基準時刻です。
            source_frame_ts_us = iterator_start_ts_us + source_frame_index * frame_period_us

            # 同期開始より前のイベント、指定duration後のイベントを落とします。
            if events.size:
                keep_time = events["t"] >= output_start_ts_us
                if output_end_ts_us is not None:
                    keep_time &= events["t"] < output_end_ts_us
                events = events[keep_time]

            # LEDや不要領域のイベントを落とします。
            events = remove_masked_events(events, mask_rois)

            # 必要なら、フィルタ後イベントを後で連結できるよう保持します。
            if args.export_filtered_events_npz and events.size:
                filtered_event_chunks.append(events.copy())

            # 空かつ完全に同期開始前のスライスは出力しません。
            if source_frame_ts_us + frame_period_us <= output_start_ts_us:
                continue

            # 指定durationを越えたら処理終了します。
            if output_end_ts_us is not None and source_frame_ts_us >= output_end_ts_us:
                break

            # このフレームの基準時刻を、出力開始からの相対マイクロ秒として作ります。
            frame_ts_us = max(0, source_frame_ts_us - output_start_ts_us)

            # PNG 連番の保存先です。保存しない場合は None のままです。
            image_path = None

            if args.save_png or video_writer is not None:
                # Metavision の描画先になる BGR 画像バッファを作ります。
                frame = np.empty((int(height), int(width), 3), dtype=np.uint8)

                # イベント列を 1 枚の確認用画像へ描画します。
                PeriodicFrameGenerationAlgorithm.generate_frame(
                    events,
                    frame,
                    int(args.accumulation_us),
                    ColorPalette.Dark,
                )

                # PNG 保存が有効ならフレーム画像を書き出します。
                if args.save_png:
                    # フレーム番号を 6 桁にして、並び順が崩れないファイル名にします。
                    image_path = frames_dir / f"frame_{output_frame_index:06d}.png"

                    # OpenCV で PNG として保存します。
                    cv2.imwrite(str(image_path), frame)

                # 動画保存が有効なら MP4 に 1 フレーム追加します。
                if video_writer is not None:
                    video_writer.write(frame)

            # CSV へこのフレームの時刻とイベント数を書きます。
            writer.writerow(
                make_frame_row(
                    output_frame_index,
                    frame_ts_us,
                    events,
                    image_path,
                    source_frame_ts_us=source_frame_ts_us,
                    sync_ts_us=sync_ts_us,
                )
            )

            # 50 フレームごとに進捗を表示します。
            if (args.save_png or video_writer is not None) and output_frame_index % 50 == 0:
                print(f"Rendered frame {output_frame_index}")

            # 出力フレーム番号を進めます。
            output_frame_index += 1

    # 動画 writer を作った場合は、ファイルを閉じて書き込みを確定します。
    if video_writer is not None:
        video_writer.release()

    # フィルタ後イベント列を npz として保存します。
    if filtered_events_path is not None:
        if filtered_event_chunks:
            filtered_events = np.concatenate(filtered_event_chunks)
        else:
            filtered_events = np.empty(0, dtype=[("x", "<u2"), ("y", "<u2"), ("p", "u1"), ("t", "<i8")])
        np.savez_compressed(
            filtered_events_path,
            events=filtered_events,
            sync_ts_us=-1 if sync_ts_us is None else int(sync_ts_us),
            output_start_ts_us=int(output_start_ts_us),
            output_end_ts_us=-1 if output_end_ts_us is None else int(output_end_ts_us),
            mask_rois=np.array(mask_rois, dtype=np.int32),
        )

    # 変換結果の場所を表示します。
    print(f"RAW: {raw_path}")
    print(f"CSV: {csv_path}")
    print(f"Render FPS: {float(args.fps):g}")
    print(f"Video playback FPS: {video_fps:g}")
    print(f"Accumulation: {int(args.accumulation_us)} us")
    print(f"Output start: {output_start_ts_us} us")
    if output_end_ts_us is not None:
        print(f"Output end: {output_end_ts_us} us")
    if mask_rois:
        print(f"Masked ROIs: {mask_rois}")
    if sync_json_path is not None:
        print(f"LED sync JSON: {sync_json_path}")
    if filtered_events_path is not None:
        print(f"Filtered events NPZ: {filtered_events_path}")

    # MP4 を作った場合は動画パスも表示します。
    if not args.no_video:
        print(f"MP4: {video_path}")

    # PNG を作った場合はフレームフォルダも表示します。
    if args.save_png:
        print(f"PNG frames: {frames_dir}")


# このファイルが直接実行されたときだけ main() を呼びます。
if __name__ == "__main__":
    # 実際の後処理を開始します。
    main()
