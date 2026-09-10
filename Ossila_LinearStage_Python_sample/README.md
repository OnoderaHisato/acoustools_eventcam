# OssilaStage

(UNOFFICIAL!) Python library to control Ossila Linear Stages via USB (serial connection). Provides a high-level, user-friendly interface for multi-axis motion, global coordinate transforms, and raster scanning.

---

## Overview

This package is designed to simplify the control of Ossila linear stages for scientific automation, especially when using multiple axes and custom coordinate conventions. It abstracts away hardware details, lets you work in a user-friendly global coordinate system, and supports raster scanning patterns.

- **Global coordinates**: You specify all positions relative to the *center* of the stage (datum = 0), regardless of the hardware's absolute origin.
- **Axis flipping**: Each axis can be flipped (+1 or -1) to match your lab's coordinate conventions, as configured in `config.md`.
- **Multi-axis support**: Move and scan in X, Y, (and Z) simultaneously.
- **Raster scanning**: Easily set up 2D scan patterns with user-friendly parameters.

---

## Directory Structure

- `ossila_stage/stage.py` - Main logic: stage communication, coordinate transforms, and raster scan classes.
- `ossila_stage/config.py` - Parses axis configuration from `config.md`.
- `config.md` - User-editable config file specifying axis serials and flip directions.
- `example.py` - Minimal example: move the stage in global coordinates.
- `example_raster.py` - Example: perform a raster scan in global coordinates.

---

## Configuration: `config.md`

Each axis is configured with a serial number and a direction (+1 or -1):

```
# Format: <AXIS> <SERIAL_NUMBER> [DIRECTION]
X E662608797769C2B -1
Y E662608797387B2E -1
Z -
```
- `DIRECTION`: +1 (no flip), -1 (flip axis to match your global convention)
- Axes with `-` as serial are ignored.

---

## Coordinate System

- **Global/user coordinates**: Centered at datum = 0 (corresponds to hardware 100 mm, for 200 mm stages).
    - E.g. `x=0` is stage center, `x=+20` is 20 mm right of center, `x=-20` is 20 mm left of center (after axis flip).
- **Stage/hardware coordinates**: Native to the hardware, 0–200 mm (for 200 mm stages).
- **Conversion**: All global moves are mapped as:
    - `stage_absolute = datum + (global * direction)`
    - Default `datum = 100` for each axis (center of travel)

---

## Main Classes in `stage.py`

### `OssilaStage`
- Controls a single axis (serial communication, move, goto, home, etc.)
- Handles direction flip for relative moves.

### `MultiAxisStage`
- Manages multiple axes (x, y, z)
- Reads axis config from `config.md`
- Provides high-level methods:
    - `move_to_global(x=..., y=..., ...)` — Move to a global (user) position (relative to center)
    - `move_by_global(x=..., y=..., ...)` — Move by a global (user) offset
    - `return_to_center()` — Move all axes to center (global 0)

### `Raster2DTrajectory`
- Generates and executes 2D raster scan patterns
- All scan points are specified in global coordinates, relative to datum=0 (stage center)
- Methods:
    - `go_to_center()` — Move to scan center
    - `go_to_first_point()`, `go_to_next_point()` — Step through scan points
    - `current_point()` — Get current scan point (global coordinates)
    - `current_relative_point()` — Grid indices of current point
    - `current_offset_from_center()` — Offset from scan center

---

## Example Usage

### 1. Minimal Example: Move in Global Coordinates
```python
from ossila_stage.stage import MultiAxisStage
from ossila_stage.config import parse_config_md

axes_config = parse_config_md('config.md')
stage = MultiAxisStage(axes_config)

# Move to center (global 0, 0)
stage.move_to_global(x=0, y=0)

# Move +20 mm in global X (relative to center)
stage.move_to_global(x=20, y=0)

# Move -20 mm in global Y (relative to center)
stage.move_to_global(x=0, y=-20)

stage.close()
```

### 2. Raster Scan Example
```python
from ossila_stage.stage import MultiAxisStage, Raster2DTrajectory
from ossila_stage.config import parse_config_md

axes_config = parse_config_md('config.md')
stage = MultiAxisStage(axes_config)

raster = Raster2DTrajectory(
    stage,
    center=(0, 0),           # Centered at stage center
    scan_size_x=50,          # mm
    scan_size_y=50,          # mm
    num_points_x=5,
    num_points_y=5,
    wait_mode='auto'
)

raster.go_to_center()
raster.go_to_first_point()
while not raster.is_finished():
    raster.go_to_next_point()
    print(raster.current_point())

stage.close()
```

---

## Best Practices
- Always use global coordinates in your scripts; let the library handle all conversions.
- Set axis flips in `config.md` to match your experimental/lab convention.
- Use `center=(0,0)` for scans centered at the stage center; use offsets for off-center scans.
- For custom stage lengths or datums, modify `MultiAxisStage`'s `axis_datum` as needed.

---

## Requirements
- Python 3.7+
- `pyserial`
- Ossila linear stage hardware

---

## License
MIT

---

**Author:** Tatsuki Fushimi, Ph.D.  
Assistant Professor, University of Tsukuba  
2nd June 2025
