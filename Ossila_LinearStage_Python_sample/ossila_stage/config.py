# ossila_stage/config.py

def parse_config_md(path):
    """
    Parse config.md for axes config.
    Format per line: <AXIS> <SERIAL_NUMBER> [DIRECTION]
    Example:
        X E662608797769C2B +1
        Y E662608797387B2E -1
        Z -
    DIRECTION is optional; defaults to +1 if not specified.
    Returns a dict suitable for MultiAxisStage.
    """
    axes_config = {}
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue  # Skip comments and blank lines
            parts = line.split()
            if len(parts) >= 2:
                axis = parts[0].lower()
                serial_number = parts[1]
                if serial_number == '-':
                    continue  # Ignore axes with '-' as serial number
                # parse direction, default +1
                if len(parts) > 2 and parts[2] in ['+1', '-1', '1', '-1']:
                    direction = int(parts[2])
                else:
                    direction = 1
                axes_config[axis] = {
                    'serial_number': serial_number,
                    'direction': direction
                }
    return axes_config
