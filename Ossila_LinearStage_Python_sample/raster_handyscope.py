import os
import sys
import time
import json
from datetime import datetime
import numpy as np
import libtiepie
import argparse
from ossila_stage.stage import MultiAxisStage, Raster2DTrajectory
from ossila_stage.config import parse_config_md

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.md')

def prompt_metadata():
    print("--- Experimental Metadata ---")
    mic_sens = input("Enter microphone sensitivity (mV/Pa): ")
    notes = input("Enter experimental notes: ")
    start_time = datetime.now().isoformat()
    return mic_sens, notes, start_time

def init_handyscope():
    libtiepie.network.auto_detect_enabled = True
    libtiepie.device_list.update()
    osc = None
    for item in libtiepie.device_list:
        if item.can_open(libtiepie.DEVICETYPE_OSCILLOSCOPE):
            osc = item.open_oscilloscope()
            break
    if not osc:
        print("No oscilloscope found.")
        sys.exit(1)
    # Setup
    osc.measure_mode = libtiepie.MM_BLOCK
    osc.sample_rate = 1e6
    osc.record_length = 10000
    osc.pre_sample_ratio = 0
    # Both channels
    for ch in osc.channels:
        ch.enabled = True
        ch.range = 8
        ch.coupling = libtiepie.CK_DCV
        ch.trigger.enabled = False
    # Trigger on ch1
    ch1 = osc.channels[0]
    ch1.trigger.enabled = True
    ch1.trigger.kind = libtiepie.TK_RISINGEDGE
    ch1.trigger.levels[0] = 0.5
    ch1.trigger.hystereses[0] = 0.05
    osc.trigger.timeout = 1
    return osc

def measure_handyscope(osc):
    osc.start()
    # 各フェーズの時間計測開始
    start_wait = time.time()
    while not osc.is_data_ready:
        time.sleep(0.01)
    end_wait = time.time()
    start_get = time.time()
    data = osc.get_data()
    end_get = time.time()
    ch1 = data[0]
    ch2 = data[1] if len(data) > 1 else None
    start_conv = time.time()
    t_arr = np.linspace(0, len(ch1) / osc.sample_rate, len(ch1))
    t_list = t_arr.tolist()
    ch1_list = ch1.tolist()
    ch2_list = ch2.tolist() if ch2 is not None else None
    end_conv = time.time()
    # 各フェーズの所要時間を毎ループ出力
    wait_time = end_wait - start_wait
    get_time = end_get - start_get
    conv_time = end_conv - start_conv
    total_time = end_conv - start_wait
    print(f"[Profiling] wait={wait_time:.3f}s, get={get_time:.3f}s, conv={conv_time:.3f}s, total={total_time:.3f}s")
    return t_list, ch1_list, ch2_list

def main():
    from datetime import datetime
    print("\n--- Raster Handyscope Scan ---")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_output = f"raster_handyscope_{timestamp}.json"
    output_path = input(f"Enter output file path (default: {default_output}): ").strip()
    if not output_path:
        output_path = default_output

    array_input_voltage = input("Enter array input voltage (V): ").strip()
    mic_sens, notes, start_time = prompt_metadata()
    # Load config and initialize stage
    axes_config = parse_config_md(CONFIG_PATH)
    stage = MultiAxisStage(axes_config)

    center = (-21.5, 4, -8.5)  # Center in global coordinates, relative to datum=0 (stage center, 100 mm)
    scan_size_x = 160     # mm, total width
    scan_size_y = 0     # mm, total height
    scan_size_z = 200
    num_points_x = 160    # Number of points along X
    num_points_y = 0
    num_points_z = 200    # Number of points along Z

    raster = Raster2DTrajectory(
        stage,
        center=center,
        scan_size_x=scan_size_x,
        scan_size_y=scan_size_y,
        scan_size_z=scan_size_z,
        num_points_x=num_points_x,
        num_points_y=num_points_y,
        num_points_z=num_points_z,
        plane='XZ',
        wait_mode='auto'
    )

    print(f"\nMoving to scan center (global coordinates): {center}")
    raster.go_to_center()
    input("Stage is at center. Press Enter to go to first raster point...")
    raster.go_to_first_point()
    print(f"Moved to first scan point: abs={raster.current_point()}, rel={raster.current_relative_point()}")
    input("Press Enter to start raster scan and measurement...")

    oscilloscope = init_handyscope()
    results = []
    try:
        while True:
            abs_pt = raster.current_point()
            rel_pt = raster.current_relative_point()
            t, ch1, ch2 = measure_handyscope(oscilloscope)
            results.append({
                "global_relative_position": abs_pt,
                "relative_grid_index": rel_pt,
                "handyscope": {
                    "time": t,
                    "ch1_voltage": ch1,
                    "ch2_voltage": ch2
                }
            })
            if raster.is_finished():
                break
            raster.go_to_next_point()
            print(f"Moved to scan point: abs={raster.current_point()}, rel={raster.current_relative_point()}")
    finally:
        del oscilloscope
        stage.close()

    output = {
        "start_time": start_time,
        "microphone_sensitivity_mV_per_Pa": mic_sens,
        "array_input_voltage_V": array_input_voltage,
        "notes": notes,
        "raster_points": results
    }
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"All data saved to {output_path}")

if __name__ == '__main__':
    main()
