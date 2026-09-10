# example.py

import os
from ossila_stage.config import parse_config_md
from ossila_stage.stage import MultiAxisStage

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.md')

if __name__ == '__main__':
    # 1. Load config.md
    axes_config = parse_config_md(CONFIG_PATH)
    print('Loaded axes config:', axes_config)
    for axis, cfg in axes_config.items():
        print(f"Axis {axis.upper()} serial: {cfg['serial_number']}, direction: {cfg['direction']}")

    # 2. Initialize the MultiAxisStage
    stage = MultiAxisStage(axes_config)

    # 2.1 Home all axes before any absolute moves
    print("\n=== Homing all axes ===")
    stage.home(wait=True)
    input('ステージがホーム位置（原点）に移動したら Enter キーを押してください…')

    # 3. Move to the center (global 0,0,...)
    print('\nMoving all axes to global center (0, 0) → stage absolute 100 mm...')
    stage.move_to_global(x=0, y=0)
    input('Stage is at global center. Press Enter to continue...')

    # 4. Move +20 mm in global X (relative to center)
    print('\nMoving +20 mm in global X (relative to center)...')
    stage.move_to_global(x=20, y=0)
    input('Stage is at (global 20, 0). Press Enter to continue...')

    # 5. Move -20 mm in global Y (relative to center)
    print('\nMoving -20 mm in global Y (relative to center)...')
    stage.move_to_global(x=0, y=-20)
    input('Stage is at (global 0, -20). Press Enter to continue...')

    # 6. Return to center
    print('\nReturning to global center (0,0)...')
    stage.move_to_global(x=0, y=0)
    input('Stage is at global center. Press Enter to finish...')

    # 7. Close connection
    stage.close()
    print('All moves complete.')
