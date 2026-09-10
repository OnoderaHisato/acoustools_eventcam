"""Window-preview Metavision event-camera capture sample.

イベントカメラのプレビューウィンドウを表示しながら撮影します。
ウィンドウにフォーカスがある状態で Enter キーを押すと RAW 記録を開始/停止し、
Esc キーまたは Q キーを押すと終了します。

実行例:
    python eventcam_enter_capture.py
    python eventcam_enter_capture.py --serial 00000000
    python eventcam_enter_capture.py --list-devices
"""

# 型ヒントの評価を遅らせ、実行時の import 負荷や循環参照の問題を避けます。
from __future__ import annotations

# コマンドライン引数を扱うための標準ライブラリです。
import argparse

# 撮影ファイル名や summary に日時を入れるために使います。
import datetime as dt

# 各撮影区間の結果を JSON として保存するために使います。
import json

# PATH や DLL 探索ディレクトリなど、実行環境の設定に使います。
import os

# OS に依存しにくい形でファイルパスを扱うために使います。
from pathlib import Path

# 型ヒントで Metavision SDK のオブジェクトをゆるく表すために使います。
from typing import Any


# os.add_dll_directory が返すハンドルを保持し、DLL 探索設定が途中で消えないようにします。
_DLL_DIRECTORY_HANDLES: list[Any] = []

# すでに os.add_dll_directory に登録したパスを覚えて、同じパスを重複登録しないようにします。
_DLL_DIRECTORY_PATHS: set[str] = set()


# 環境変数のパスリスト先頭に、存在するディレクトリだけを重複なしで追加する関数です。
def prepend_environment_paths(variable_name: str, directories: list[Path]) -> None:
    # いま設定されている環境変数を OS 標準の区切り文字で分解します。
    existing_parts = [part for part in os.environ.get(variable_name, "").split(os.pathsep) if part]

    # 大文字小文字や相対パス差を吸収したキーを作り、重複判定に使います。
    seen = {os.path.normcase(os.path.abspath(part)) for part in existing_parts}

    # 追加すべき新しいパス文字列を順番に貯めます。
    new_parts: list[str] = []

    # 指定された候補ディレクトリを 1 つずつ確認します。
    for directory in directories:
        # 実在しないディレクトリは環境変数に入れても意味がないのでスキップします。
        if not directory.exists():
            continue

        # 絶対パスにして、実行場所に依存しない値にします。
        resolved = str(directory.resolve())

        # 重複判定用に正規化したキーを作ります。
        key = os.path.normcase(os.path.abspath(resolved))

        # すでに入っているパスなら追加しません。
        if key in seen:
            continue

        # 新規パスとして追加予定リストへ入れます。
        new_parts.append(resolved)

        # 以降の候補でも重複を避けるため、見たパスとして登録します。
        seen.add(key)

    # 追加するものがない場合は、環境変数を書き換えません。
    if not new_parts:
        return

    # 新しいパスを先頭に置き、既存のパスを後ろへ残します。
    os.environ[variable_name] = os.pathsep.join(new_parts + existing_parts)


# このリポジトリ内にある OpenEB の DLL / HAL plugin をスクリプト実行時に見つけられるようにする関数です。
def configure_local_metavision_environment() -> None:
    # このスクリプトが置かれているフォルダを、プロジェクトルートとして扱います。
    project_dir = Path(__file__).resolve().parent

    # 以前ビルドした OpenEB の install 先を指します。
    openeb_install_dir = project_dir / "openeb_install"

    # Metavision の DLL 群が入っている bin ディレクトリです。
    openeb_bin_dir = openeb_install_dir / "bin"

    # vcpkg 由来の依存 DLL 群が入っている bin ディレクトリです。
    vcpkg_bin_dir = project_dir / "vcpkg" / "vcpkg_installed" / "x64-windows" / "bin"
    if not vcpkg_bin_dir.exists():
        vcpkg_bin_dir = project_dir / "vcpkg" / "installed" / "x64-windows" / "bin"

    # HAL がカメラ用 plugin DLL を探すディレクトリです。
    hal_plugin_dir = openeb_install_dir / "lib" / "metavision" / "hal" / "plugins"

    # SilkyEvCam installer が入れる CenturyArks 公式 plugin / 依存 DLL の場所です。
    centuryarks_dir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "CenturyArks"
    centuryarks_bin_dir = centuryarks_dir / "bin"
    centuryarks_plugin_dir = centuryarks_dir / "plugins"

    # HDF5 圧縮 plugin がある場合に備えた探索ディレクトリです。
    hdf5_plugin_dir = openeb_install_dir / "lib" / "hdf5" / "plugin"

    # 通常の DLL 探索用に PATH の先頭へローカルビルドのディレクトリを追加します。
    prepend_environment_paths("PATH", [openeb_bin_dir, vcpkg_bin_dir, hal_plugin_dir, centuryarks_bin_dir, centuryarks_plugin_dir])

    # Metavision HAL が plugin DLL を探せるように専用環境変数も設定します。
    prepend_environment_paths("MV_HAL_PLUGIN_PATH", [hal_plugin_dir, centuryarks_plugin_dir])

    # RAW/HDF5 関連の plugin が必要になった場合に備えて HDF5 側の探索パスも設定します。
    prepend_environment_paths("HDF5_PLUGIN_PATH", [hdf5_plugin_dir])

    # Windows の Python では PATH だけで DLL import できないことがあるため、明示的に登録します。
    if hasattr(os, "add_dll_directory"):
        # Metavision import 前に必要になり得る DLL ディレクトリを順番に登録します。
        for directory in [openeb_bin_dir, vcpkg_bin_dir, hal_plugin_dir, centuryarks_bin_dir, centuryarks_plugin_dir]:
            # 存在しないディレクトリは登録できないのでスキップします。
            if not directory.exists():
                continue

            # 絶対パス文字列へ変換します。
            resolved = str(directory.resolve())

            # 同じディレクトリを重複登録しないようにします。
            if resolved in _DLL_DIRECTORY_PATHS:
                continue

            # DLL 探索ディレクトリとして Python プロセスに登録します。
            handle = os.add_dll_directory(resolved)

            # 返されたハンドルを保持し、探索設定の寿命をプロセス終了まで伸ばします。
            _DLL_DIRECTORY_HANDLES.append(handle)

            # 登録済みパスとして覚えておきます。
            _DLL_DIRECTORY_PATHS.add(resolved)


# Metavision SDK の Python binding を読み込む関数です。
def import_metavision() -> tuple[Any, ...]:
    # VS Code や PowerShell から venv の python.exe を直指定しても動くよう、OpenEB の探索パスを先に整えます。
    configure_local_metavision_environment()

    # SDK が見つからない場合に、分かりやすいエラーへ変換するため try にします。
    try:
        # イベントを一定時間ごとの配列として読み出す iterator です。
        from metavision_core.event_io import EventsIterator

        # カメラ serial や空文字から HAL device を開く関数です。
        from metavision_core.event_io.raw_reader import initiate_device

        # カメラ一覧取得などに使う Metavision HAL モジュールです。
        import metavision_hal

        # イベント列を人間が見られるフレーム画像へ変換するアルゴリズムです。
        from metavision_sdk_core import PeriodicFrameGenerationAlgorithm, ColorPalette

        # プレビューウィンドウとキー入力イベントを扱う UI モジュールです。
        from metavision_sdk_ui import EventLoop, BaseWindow, MTWindow, UIAction, UIKeyEvent

    # import に失敗した場合は、venv / PATH / UI module 設定が不足している可能性があります。
    except Exception as exc:
        # SystemExit にすることで、ユーザー向けの短いメッセージで終了できます。
        raise SystemExit(
            "Metavision Python/UI bindings are not available. "
            "Run .\\activate_metavision_env.ps1 first, or verify openeb_install and "
            "vcpkg\\vcpkg_installed exist. "
            f"Original error: {exc}"
        ) from exc

    # 呼び出し側で使う API 群を返します。
    return (
        initiate_device,
        EventsIterator,
        metavision_hal,
        PeriodicFrameGenerationAlgorithm,
        ColorPalette,
        EventLoop,
        BaseWindow,
        MTWindow,
        UIAction,
        UIKeyEvent,
    )


# コマンドライン引数を定義して読み取る関数です。
def parse_args() -> argparse.Namespace:
    # help 表示にデフォルト値も出る argparse parser を作ります。
    parser = argparse.ArgumentParser(
        description="Preview an event camera and start/stop RAW recording with Enter.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # カメラの serial/device id です。空文字なら最初に見つかったカメラを開きます。
    parser.add_argument("--serial", default="", help="Camera serial/device id. Empty opens the first camera.")

    # EventsIterator が 1 回に返すイベント配列の時間幅をマイクロ秒で指定します。
    parser.add_argument("--delta-t-us", type=int, default=10_000, help="Event slice duration in microseconds.")

    # プレビュー表示の更新 FPS です。
    parser.add_argument("--preview-fps", type=float, default=25.0, help="Preview frame rate.")

    # RAW と summary JSON を保存するディレクトリです。
    parser.add_argument("--output-dir", default="recordings_eventcam", help="Directory for output files.")

    # 出力ファイル名の先頭部分です。後ろに日時と撮影番号を付けます。
    parser.add_argument("--basename", default="eventcam_enter", help="Output filename prefix.")

    # デバイス一覧だけ表示して終了するモードです。
    parser.add_argument("--list-devices", action="store_true", help="List detected devices and exit.")

    # キー操作やプレビュー確認だけしたいとき、RAW を保存しないで統計だけ取るモードです。
    parser.add_argument("--stats-only", action="store_true", help="Do not write RAW files.")

    # 実際に指定された引数を Namespace として返します。
    return parser.parse_args()


# 接続されている Metavision 対応デバイスを表示する関数です。
def list_devices(metavision_hal: Any) -> None:
    # HAL の DeviceDiscovery でデバイス一覧を取得します。
    devices = metavision_hal.DeviceDiscovery.list()

    # 表示用の見出しです。
    print("Detected devices:")

    # 1 台も見つからない場合は none と表示します。
    if not devices:
        print("  none")
        return

    # 見つかったデバイスを 1 行ずつ表示します。
    for device in devices:
        print(f"  {device}")


# 新しい RAW / JSON 出力パスを作る関数です。
def make_output_paths(out_dir: Path, basename: str, capture_index: int) -> tuple[Path, Path]:
    # ファイル名に現在時刻を入れ、撮影ごとに重ならないようにします。
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    # basename、時刻、撮影番号を組み合わせた共通 stem を作ります。
    stem = out_dir / f"{basename}_{stamp}_{capture_index:03d}"

    # RAW イベントファイルのパスを作ります。
    raw_path = stem.with_suffix(".raw")

    # 撮影概要 JSON のパスを作ります。
    summary_path = stem.with_suffix(".json")

    # 2 つのパスを呼び出し元へ返します。
    return raw_path, summary_path


# 撮影区間の統計を初期化する関数です。
def new_capture_stats(raw_path: Path | None, summary_path: Path, sensor_size: dict[str, int]) -> dict[str, Any]:
    # 1 回分の撮影結果を保存する辞書を作ります。
    return {
        # RAW を保存する場合はパス、保存しない場合は None です。
        "raw_path": None if raw_path is None else str(raw_path.resolve()),

        # summary JSON 自身の保存先です。
        "summary_path": str(summary_path.resolve()),

        # センサー解像度です。
        "sensor_size": sensor_size,

        # Enter で記録開始した壁時計時刻です。
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),

        # Enter で記録停止した壁時計時刻です。停止時に埋めます。
        "stopped_at": None,

        # この撮影区間で処理したイベントスライス数です。
        "total_slices": 0,

        # この撮影区間で受け取ったイベント総数です。
        "total_events": 0,

        # この撮影区間で最初に受け取ったイベント時刻です。
        "first_event_ts_us": None,

        # この撮影区間で最後に受け取ったイベント時刻です。
        "last_event_ts_us": None,
    }


# 撮影中の統計に、今回のイベントスライスを加算する関数です。
def update_capture_stats(stats: dict[str, Any], events: Any) -> None:
    # スライスを 1 つ処理したのでカウントします。
    stats["total_slices"] += 1

    # このスライス内のイベント数を数えます。
    count = int(events.size)

    # 総イベント数へ加算します。
    stats["total_events"] += count

    # 空スライスの場合はタイムスタンプを読めないのでここで終了します。
    if count == 0:
        return

    # その撮影区間の最初のイベント時刻だけを保存します。
    if stats["first_event_ts_us"] is None:
        stats["first_event_ts_us"] = int(events["t"][0])

    # 最後のイベント時刻は毎回更新し、最終的な最後の時刻にします。
    stats["last_event_ts_us"] = int(events["t"][-1])


# 撮影停止時に RAW 記録を止め、summary JSON を保存する関数です。
def stop_recording(events_stream: Any, stats: dict[str, Any], summary_path: Path, stats_only: bool) -> None:
    # RAW 保存している場合だけ、HAL に記録停止を指示します。
    if not stats_only:
        events_stream.stop_log_raw_data()

    # 停止時刻を summary に記録します。
    stats["stopped_at"] = dt.datetime.now().isoformat(timespec="seconds")

    # summary dict を JSON 文字列へ変換し、ファイルに保存します。
    summary_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")

    # 停止したこととイベント数を表示します。
    print(f"Stopped. events={stats['total_events']}, summary={summary_path}")


# スクリプトのメイン処理です。
def main() -> None:
    # コマンドライン引数を読み取ります。
    args = parse_args()

    # delta_t が 0 以下だと iterator が正しく動けないので早めに止めます。
    if args.delta_t_us <= 0:
        raise SystemExit("--delta-t-us must be positive.")

    # preview FPS が 0 以下だとフレーム生成周期が決められないので早めに止めます。
    if args.preview_fps <= 0:
        raise SystemExit("--preview-fps must be positive.")

    # Metavision の必要な API を読み込みます。
    (
        initiate_device,
        EventsIterator,
        metavision_hal,
        PeriodicFrameGenerationAlgorithm,
        ColorPalette,
        EventLoop,
        BaseWindow,
        MTWindow,
        UIAction,
        UIKeyEvent,
    ) = import_metavision()

    # --list-devices 指定なら、撮影せずデバイス一覧だけ表示します。
    if args.list_devices:
        list_devices(metavision_hal)
        return

    # 出力ディレクトリを Path として作ります。
    out_dir = Path(args.output_dir)

    # 出力ディレクトリがなければ作成します。
    out_dir.mkdir(parents=True, exist_ok=True)

    # HAL device を finally で解放できるよう、先に None で用意します。
    device = None

    # RAW 記録を制御する I_EventsStream facility を入れる変数です。
    events_stream = None

    # イベントを読み出す EventsIterator を入れる変数です。
    iterator = None

    # いま RAW 記録中かどうかを表すフラグです。
    is_recording = False

    # 撮影ごとの連番です。
    capture_index = 0

    # 現在記録中の RAW パスです。
    current_raw_path: Path | None = None

    # 現在記録中の summary JSON パスです。
    current_summary_path: Path | None = None

    # 現在記録中の統計辞書です。
    current_stats: dict[str, Any] | None = None

    # カメラやファイルを扱うため、終了時に必ずクリーンアップできるよう try/finally にします。
    try:
        # カメラを開くことを表示します。
        print("Opening event camera...")

        # カメラが開けない場合に、検出状況も含めた分かりやすい表示へ変換します。
        try:
            # serial が空なら最初のカメラ、指定ありならそのカメラを開きます。
            device = initiate_device(path=args.serial)

        # initiate_device はカメラが見つからない場合などに OSError を投げます。
        except OSError as exc:
            # HAL が現在認識しているデバイス一覧を取得します。
            detected_devices = metavision_hal.DeviceDiscovery.list()

            # 一覧が空なら none、何かあれば 1 行文字列へまとめます。
            detected_text = "none" if not detected_devices else ", ".join(str(device) for device in detected_devices)

            # serial 指定がある場合は、その指定値もメッセージに入れます。
            serial_text = args.serial if args.serial else "(first detected camera)"

            # 何を確認すべきかが分かる短いエラーとして終了します。
            raise SystemExit(
                "Could not open the event camera.\n"
                f"Requested device: {serial_text}\n"
                f"Detected devices: {detected_text}\n"
                f"Original error: {exc}\n"
                "Check the USB connection, Windows driver, camera power, and whether this camera is supported by the installed OpenEB plugin."
            ) from exc

        # 開いた device から RAW データ記録用の I_EventsStream facility を取得します。
        events_stream = device.get_i_events_stream()

        # facility がない場合は RAW 記録できないので終了します。
        if events_stream is None:
            raise RuntimeError("This device does not expose I_EventsStream.")

        # device からイベントを読み続ける iterator を作ります。
        iterator = EventsIterator.from_device(
            # すでに開いた HAL device を使います。
            device=device,

            # delta_t モードは、一定時間ごとにイベント配列を返します。
            mode="delta_t",

            # 1 スライスの時間幅を指定します。
            delta_t=args.delta_t_us,

            # None にすると、ウィンドウが閉じられるまで読み続けます。
            max_duration=None,

            # False にすると、イベント時刻を相対値に変換せず元時刻として扱います。
            relative_timestamps=False,
        )

        # センサー解像度を取得します。戻り値は height, width の順です。
        height, width = iterator.get_size()

        # summary JSON に入れるため、解像度を dict にします。
        sensor_size = {"width": int(width), "height": int(height)}

        # 起動後の操作方法を表示します。
        print(f"Sensor: {width} x {height}")

        # Enter と Esc の操作説明を表示します。
        print("Window controls: Enter=start/stop recording, Esc/Q=quit.")

        # だいたい 1 秒ごとに進捗を出すためのスライス数を計算します。
        progress_every = max(1, int(1_000_000 / args.delta_t_us))

        # イベントをプレビュー画像へ変換するフレーム生成器を作ります。
        frame_generator = PeriodicFrameGenerationAlgorithm(
            # センサーの横幅です。
            sensor_width=width,

            # センサーの縦幅です。
            sensor_height=height,

            # プレビュー表示の更新 FPS です。
            fps=args.preview_fps,

            # 暗背景にイベントを描く Metavision 標準パレットです。
            palette=ColorPalette.Dark,
        )

        # Metavision UI のウィンドウを作り、with を抜けると自動で閉じるようにします。
        with MTWindow(
            # ウィンドウタイトルです。
            title="Event Camera Preview - Enter: REC / STOP, Esc: Quit",

            # ウィンドウ横幅です。
            width=width,

            # ウィンドウ縦幅です。
            height=height,

            # PeriodicFrameGenerationAlgorithm が作る BGR 画像を表示する指定です。
            mode=BaseWindow.RenderMode.BGR,
        ) as window:
            # フレーム生成器から画像が出たときに呼ばれる callback です。
            def on_frame(_: int, frame: Any) -> None:
                # 生成されたフレームをウィンドウへ非同期表示します。
                window.show_async(frame)

            # フレーム生成器に callback を登録します。
            frame_generator.set_output_callback(on_frame)

            # キーボード入力があったときに呼ばれる callback です。
            def keyboard_cb(key: Any, scancode: int, action: Any, mods: int) -> None:
                # 外側の撮影状態を変更するため nonlocal 宣言します。
                nonlocal is_recording

                # 外側の撮影番号を変更するため nonlocal 宣言します。
                nonlocal capture_index

                # 外側の現在 RAW パスを変更するため nonlocal 宣言します。
                nonlocal current_raw_path

                # 外側の現在 summary パスを変更するため nonlocal 宣言します。
                nonlocal current_summary_path

                # 外側の現在統計を変更するため nonlocal 宣言します。
                nonlocal current_stats

                # キーを押した瞬間だけ処理し、押しっぱなしの repeat は無視します。
                if action != UIAction.PRESS:
                    return

                # Esc または Q が押されたら終了します。
                if key == UIKeyEvent.KEY_ESCAPE or key == UIKeyEvent.KEY_Q:
                    # 記録中なら終了前に停止して summary を保存します。
                    if is_recording and current_stats is not None and current_summary_path is not None:
                        stop_recording(events_stream, current_stats, current_summary_path, args.stats_only)

                    # 記録中フラグを下ろします。
                    is_recording = False

                    # ウィンドウへ閉じる要求を出します。
                    window.set_close_flag()

                    # Esc/Q の処理はここで終わります。
                    return

                # Enter またはテンキー Enter が押されたら開始/停止を切り替えます。
                if key == UIKeyEvent.KEY_ENTER or key == UIKeyEvent.KEY_KP_ENTER:
                    # 記録中なら Enter で停止します。
                    if is_recording:
                        # 現在の summary パスがない状態は異常なので RuntimeError にします。
                        if current_stats is None or current_summary_path is None:
                            raise RuntimeError("Internal recording state is incomplete.")

                        # RAW 記録停止と summary 保存を行います。
                        stop_recording(events_stream, current_stats, current_summary_path, args.stats_only)

                        # 記録中フラグを下ろします。
                        is_recording = False

                        # 現在の撮影状態をクリアします。
                        current_stats = None
                        current_raw_path = None
                        current_summary_path = None

                    # 記録していないなら、Enter で新しい撮影を開始します。
                    else:
                        # 撮影番号を 1 つ進めます。
                        capture_index += 1

                        # 今回の RAW / JSON 出力パスを作ります。
                        raw_path, summary_path = make_output_paths(out_dir, args.basename, capture_index)

                        # stats-only の場合は RAW パスを None として統計だけ取ります。
                        current_raw_path = None if args.stats_only else raw_path

                        # 今回の summary JSON パスを保持します。
                        current_summary_path = summary_path

                        # 今回の撮影統計を初期化します。
                        current_stats = new_capture_stats(current_raw_path, summary_path, sensor_size)

                        # RAW 保存する場合は HAL に記録開始を指示します。
                        if not args.stats_only:
                            events_stream.log_raw_data(str(raw_path.resolve()))

                        # 記録中フラグを立てます。
                        is_recording = True

                        # 開始したことを表示します。
                        print(f"Started recording #{capture_index}: {raw_path}")

            # ウィンドウにキーボード callback を登録します。
            window.set_keyboard_callback(keyboard_cb)

            # カメラからイベントを読み続けます。
            for events in iterator:
                # OS/ウィンドウシステムのイベントを処理し、キー入力 callback を発火させます。
                EventLoop.poll_and_dispatch()

                # 今回のイベントスライスをプレビュー画像生成器へ渡します。
                frame_generator.process_events(events)

                # 記録中だけ、イベント数やタイムスタンプを統計へ加算します。
                if is_recording and current_stats is not None:
                    # 現在のイベントスライスを統計へ反映します。
                    update_capture_stats(current_stats, events)

                    # だいたい 1 秒ごとに進捗を表示します。
                    if current_stats["total_slices"] % progress_every == 0:
                        print(
                            "Recording... "
                            f"slices={current_stats['total_slices']}, "
                            f"events={current_stats['total_events']}"
                        )

                # Esc/Q またはウィンドウの閉じるボタンで close flag が立ったら終了します。
                if window.should_close():
                    break

    # 正常終了でもエラーでも、記録停止と device 解放を試みます。
    finally:
        # 記録中に例外などで抜けた場合でも RAW を閉じます。
        if is_recording and events_stream is not None and current_stats is not None and current_summary_path is not None:
            # 終了処理中の例外で元のエラーを隠さないよう try にします。
            try:
                # RAW 記録停止と summary 保存を行います。
                stop_recording(events_stream, current_stats, current_summary_path, args.stats_only)

            # 終了処理中の例外は握りつぶします。
            except Exception:
                pass

        # iterator を明示的に捨て、内部の camera 参照を解放しやすくします。
        del iterator

        # device を明示的に捨て、カメラ接続を閉じやすくします。
        del device


# このファイルが直接実行されたときだけ main() を呼びます。
if __name__ == "__main__":
    # 実際の処理を開始します。
    main()
