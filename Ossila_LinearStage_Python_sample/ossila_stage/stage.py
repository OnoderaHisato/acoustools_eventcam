import serial
import serial.tools.list_ports
import time
import numpy as np

# Default serial settings for Ossila linear stages
DEFAULT_BAUDRATE = 9600
DEFAULT_TIMEOUT = 0.05
MOTION_TIMEOUT_SEC = 60  # Reasonable default for stage moves

class OssilaStage:
    def __init__(self, serial_number, baudrate=DEFAULT_BAUDRATE, timeout=DEFAULT_TIMEOUT, direction=1):
        """
        Initialize a single-axis stage by matching its serial number to a COM port.
        """
        self.serial_number = serial_number
        self.direction = direction  # +1 or -1
        self.port = self._find_port_by_serial(serial_number)
        if not self.port:
            raise RuntimeError(f"Serial number {serial_number} not found.")
        self.ser = serial.Serial(self.port, baudrate=baudrate, timeout=timeout)

    def _find_port_by_serial(self, target_serial):
        """
        Enumerate all connected serial devices and match by serial_number or HWID.
        Returns the device string (e.g., 'COM3') or None.
        """
        for port in serial.tools.list_ports.comports():
            if port.serial_number == target_serial:
                return port.device
            if target_serial in (port.hwid or ""):
                return port.device
        return None

    def _send_command(self, command, delay=0.1):
        """
        Send a command wrapped in angle brackets and read up to 100 bytes of response.
        """
        full_command = f"<{command.lower()}>"
        self.ser.write(full_command.encode())
        time.sleep(delay)
        return self.ser.read(100).decode(errors="ignore").strip()

    def stop(self):
        return self._send_command("stop")

    def hardstop(self):
        return self._send_command("hardstop")

    def stophiz(self):
        return self._send_command("stophiz")

    def hardstophiz(self):
        return self._send_command("hardstophiz")

    def home(self):
        return self._send_command("home")

    def move(self, distance, speed=None, wait=True, wait_mode="auto", wait_time=0.1):
        distance = float(distance) * self.direction
        if speed is not None:
            resp = self._send_command(f"move {distance} {speed}")
        else:
            resp = self._send_command(f"move {distance}")
        if wait:
            if wait_mode == "auto":
                self.wait_until_motion_complete()
            elif wait_mode == "time":
                time.sleep(wait_time)
        return resp

    def run(self, speed):
        return self._send_command(f"run {speed}")

    def goto(self, position, speed=None, wait=True, wait_mode="auto", wait_time=0.1):
        position = float(position)
        if speed is not None:
            resp = self._send_command(f"goto {position} {speed}")
        else:
            resp = self._send_command(f"goto {position}")
        if wait:
            if wait_mode == "auto":
                self.wait_until_motion_complete()
            elif wait_mode == "time":
                time.sleep(wait_time)
        return resp

    def acc(self, value):
        return self._send_command(f"acc {value}")

    def acc_query(self):
        return self._send_command("acc?")

    def dec(self, value):
        return self._send_command(f"dec {value}")

    def dec_query(self):
        return self._send_command("dec?")

    def reset(self):
        return self._send_command("reset")

    def status(self):
        return self._send_command("status?")

    def posmode(self):
        return self._send_command("posmode?")

    def pos(self):
        raw = self._send_command("pos?")
        try:
            val = float(raw)
            return val * self.direction
        except Exception:
            return raw

    def wait_until_motion_complete(self, timeout=MOTION_TIMEOUT_SEC):
        start = time.time()
        while True:
            status = self._send_command("status?")
            if status.startswith("<status"):
                try:
                    parts = status[1:-1].split()
                    speed = float(parts[1])
                    if speed == 0.0:
                        break
                except Exception:
                    pass
            if time.time() - start > timeout:
                print("❌ Timeout waiting for motion.")
                break
            time.sleep(0.001)

    def speed_query(self):
        return self._send_command("speed?")

    def alarms(self):
        return self._send_command("alarms?")

    def clearalarms(self):
        return self._send_command("clearalarms")

    def device(self):
        return self._send_command("device?")

    def length(self):
        return self._send_command("length?")

    def firmware(self):
        return self._send_command("firmware?")

    def serial(self):
        return self._send_command("serial?")

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()


class Raster2DTrajectory:
    """
    Raster scan trajectory controller for MultiAxisStage supporting arbitrary plane (XY, XZ, YZ).
    center: (x, y, z) tuple, scan_size_* and num_points_* per axis.
    plane: optional 'XY','XZ','YZ'. If None, inferred from num_points >1.
    """
    def __init__(self,
                 stage,
                 center=(0, 0, 0),
                 scan_size_x=20, scan_size_y=20, scan_size_z=20,
                 num_points_x=20, num_points_y=20, num_points_z=20,
                 plane=None,
                 wait_mode="auto", wait_time=0.1):
        self.stage = stage
        self.center = tuple(center)
        self.scan_sizes = {'x': scan_size_x, 'y': scan_size_y, 'z': scan_size_z}
        self.num_points = {'x': num_points_x, 'y': num_points_y, 'z': num_points_z}
        self.wait_mode = wait_mode
        self.wait_time = wait_time

        # Determine plane
        if plane:
            self.plane = plane.upper()
        else:
            axes = [ax.upper() for ax, n in self.num_points.items() if n > 1]
            if len(axes) != 2:
                raise ValueError(f"Cannot infer plane, expected 2 axes with num_points>1, got {axes}")
            self.plane = ''.join(axes)
        self.axis1, self.axis2 = self.plane.lower()
        self.axis3 = next(a for a in ['x', 'y', 'z'] if a not in (self.axis1, self.axis2))

        # Build trajectory
        self.trajectory = self._calculate_trajectory()
        self.index = -1

    def _calculate_trajectory(self):
        c = self.center
        base = {ax: c['xyz'.index(ax)] for ax in ['x','y','z']}
        sizes = self.scan_sizes
        counts = self.num_points
        pts1 = np.linspace(base[self.axis1] - sizes[self.axis1]/2,
                           base[self.axis1] + sizes[self.axis1]/2,
                           counts[self.axis1])
        pts2 = np.linspace(base[self.axis2] - sizes[self.axis2]/2,
                           base[self.axis2] + sizes[self.axis2]/2,
                           counts[self.axis2])
        traj = []
        for row, val2 in enumerate(pts2):
            line = pts1 if row % 2 == 0 else pts1[::-1]
            for val1 in line:
                pos = {
                    self.axis1: float(val1),
                    self.axis2: float(val2),
                    self.axis3: float(base[self.axis3])
                }
                traj.append(pos)
        return traj

    def current_relative_point(self):
        if self.index < 0 or self.index >= len(self.trajectory):
            return None
        iy = self.index // self.num_points[self.axis1]
        ix = self.index % self.num_points[self.axis1]
        if iy % 2:
            ix = self.num_points[self.axis1] - 1 - ix
        return (ix, iy, self.num_points[self.axis1], self.num_points[self.axis2])

    def current_point(self):
        if 0 <= self.index < len(self.trajectory):
            return self.trajectory[self.index]
        return None

    def current_offset_from_center(self):
        pt = self.current_point()
        if not pt:
            return None
        return (pt[self.axis1] - self.center['xyz'.index(self.axis1)],
                pt[self.axis2] - self.center['xyz'.index(self.axis2)])

    def go_to_center(self):
        axes = {ax: self.center['xyz'.index(ax)] for ax in ['x','y','z']}
        self.stage.move_to_global(wait=True, wait_mode=self.wait_mode, wait_time=self.wait_time, **axes)
        self.stage.stop()
        self.index = -1

    def go_to_first_point(self):
        if not self.trajectory:
            return
        self.index = 0
        self.stage.move_to_global(wait=True, wait_mode=self.wait_mode, wait_time=self.wait_time, **self.trajectory[0])
        self.stage.stop()

    def go_to_next_point(self):
        if self.index < 0:
            return self.go_to_first_point()
        if self.index < len(self.trajectory) - 1:
            self.index += 1
            self.stage.move_to_global(wait=True, wait_mode=self.wait_mode, wait_time=self.wait_time, **self.trajectory[self.index])
            self.stage.stop()

    def is_finished(self):
        return self.index >= len(self.trajectory) - 1

    def reset(self):
        self.index = -1


class MultiAxisStage:
    """
    Controller for multi-axis stage setups (X, XY, or XYZ).
    axes_config: dict mapping axis labels to {'serial_number': str, 'direction': ±1}
    """
    def __init__(self, axes_config, baudrate=DEFAULT_BAUDRATE, timeout=DEFAULT_TIMEOUT):
        self.axes = {}
        self.axis_flip = {}
        self.axis_datum = {axis: 100 for axis in axes_config}

        for axis, cfg in axes_config.items():
            self.axes[axis] = OssilaStage(
                serial_number=cfg['serial_number'],
                baudrate=baudrate,
                timeout=timeout,
                direction=cfg.get('direction', 1)
            )
            self.axis_flip[axis] = cfg.get('direction', 1)

    def move_to(self, wait=True, wait_mode="auto", wait_time=0.1, **positions):
        for axis, pos in positions.items():
            if axis in self.axes:
                self.axes[axis].goto(pos, wait=False,
                                     wait_mode=wait_mode,
                                     wait_time=wait_time)
        if wait:
            if wait_mode == "auto":
                for axis in positions:
                    if axis in self.axes:
                        self.axes[axis].wait_until_motion_complete()
            else:
                time.sleep(wait_time)

    def move_to_global(self, wait=True, wait_mode="auto", wait_time=0.1, **positions):
        mapped = {}
        for axis, val in positions.items():
            if axis in self.axes:
                datum = self.axis_datum[axis]
                flip = self.axis_flip[axis]
                mapped[axis] = datum + val * flip
        self.move_to(wait=wait,
                     wait_mode=wait_mode,
                     wait_time=wait_time,
                     **mapped)

    def move_by_global(self, wait=True, wait_mode="auto", wait_time=0.1, **deltas):
        flipped = {}
        for axis, d in deltas.items():
            if axis in self.axes:
                flipped[axis] = d * self.axis_flip[axis]
        self.move_by(wait=wait,
                     wait_mode=wait_mode,
                     wait_time=wait_time,
                     **flipped)

    def move_by(self, wait=True, wait_mode="auto", wait_time=0.1, **deltas):
        for axis, delta in deltas.items():
            if axis in self.axes:
                self.axes[axis].move(delta, wait=False,
                                     wait_mode=wait_mode,
                                     wait_time=wait_time)
        if wait:
            if wait_mode == "auto":
                for axis in deltas:
                    if axis in self.axes:
                        self.axes[axis].wait_until_motion_complete()
            else:
                time.sleep(wait_time)

    def get_position(self):
        return {axis: stage.pos() for axis, stage in self.axes.items()}

    def home(self, wait=True):
        for axis, stage in self.axes.items():
            stage.home()
            if wait:
                stage.wait_until_motion_complete()

    def stop(self):
        for axis, stage in self.axes.items():
            stage.stop()

    def return_to_center(self, wait=True):
        centers = {axis: 100 for axis in self.axes}
        self.move_to(wait=wait, **centers)

    def close(self):
        for axis, stage in self.axes.items():
            stage.close()
