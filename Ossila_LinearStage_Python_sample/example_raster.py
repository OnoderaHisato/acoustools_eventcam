import os
from ossila_stage.stage import MultiAxisStage, Raster2DTrajectory
from ossila_stage.config import parse_config_md

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.md')

if __name__ == '__main__':
    # 1. Load config.md
    axes_config = parse_config_md(CONFIG_PATH)
    print('Loaded axes config:', axes_config)
    for axis, cfg in axes_config.items():
        print(f"Axis {axis.upper()} serial: {cfg['serial_number']}, direction: {cfg['direction']}")

    # 2. Initialize the MultiAxisStage
    stage = MultiAxisStage(axes_config)

    # 3. Set up the raster scan parameters
    # All coordinates below are in the GLOBAL/user coordinate system, RELATIVE TO DATUM=0 (stage center, 100 mm)
    # That is, center=(0, 0) means scan is centered at the stage center.
    center = (-21.5, 4, -8.5)  # Center in global coordinates, relative to datum=0 (stage center, 100 mm)
    scan_size_x = 160     # mm, total width
    scan_size_y = 0     # mm, total height
    scan_size_z = 200
    num_points_x = 2    # Number of points along X
    num_points_y = 0
    num_points_z = 2    # Number of points along Z

    # Use wait_mode='time' for fast raster scan (waits a fixed time at each point)
    # You can set wait_mode='auto' for precise stopping, or 'none' for manual control
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
    input("Stage is at center (global coordinates). Press Enter to start raster scan...")

    raster.go_to_first_point()
    abs_pt = raster.current_point()
    rel_pt = raster.current_relative_point()
    offset_pt = raster.current_offset_from_center()
    print(f"\nMoved to first scan point (global): abs={abs_pt}, rel={rel_pt}, offset_from_center={offset_pt}")
    input("Press Enter to move to next scan point...")

    while not raster.is_finished():
        raster.go_to_next_point()
        abs_pt = raster.current_point()
        rel_pt = raster.current_relative_point()
        offset_pt = raster.current_offset_from_center()
        print(f"Moved to scan point (global): abs={abs_pt}, rel={rel_pt}, offset_from_center={offset_pt}")
        input("Press Enter to move to next scan point...")

    print("\nRaster scan complete. Returning to center (global coordinates)...")
    raster.go_to_center()
    stage.close()
    print('Stage closed. All done.')
