import logging
import subprocess
from typing import List, Tuple

logger = logging.getLogger("byconnx.mobile.adb_client")

class ADBClient:
    """
    Executes standard Android Debug Bridge (ADB) actions.
    Connects to target device and triggers touch inputs, text entry, and app launching.
    """
    def __init__(self, serial_number: str = ""):
        self.serial = serial_number
        self.cmd_prefix = ["adb"]
        if self.serial:
            self.cmd_prefix.extend(["-s", self.serial])

    def _execute(self, args: List[str]) -> Tuple[str, str]:
        """Wrapper around subprocess to run adb command shell actions."""
        full_command = self.cmd_prefix + args
        try:
            res = subprocess.run(full_command, capture_output=True, text=True, check=True)
            return res.stdout, res.stderr
        except subprocess.CalledProcessError as e:
            logger.error(f"ADB execution failed: {' '.join(full_command)} | Error: {e.stderr}")
            return "", e.stderr

    def list_devices(self) -> List[str]:
        """Queries and lists currently attached Android nodes."""
        stdout, _ = self._execute(["devices"])
        devices = []
        for line in stdout.splitlines()[1:]:
            if line.strip():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
        return devices

    def tap(self, x: int, y: int):
        """Triggers single finger tap on specific screen coordinate."""
        logger.info(f"Injecting tap: ({x}, {y})")
        self._execute(["shell", "input", "tap", str(x), str(y)])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500):
        """Injects touch drag actions (e.g. for list/page scrolling)."""
        logger.info(f"Injecting swipe from ({x1}, {y1}) -> ({x2}, {y2}) over {duration_ms}ms")
        self._execute(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)])

    def type_text(self, text: str):
        """Inputs raw text into active input field focus area."""
        # Replace whitespaces with escape format for adb compatibility
        formatted_text = text.replace(" ", "%s")
        logger.info(f"Injecting text inputs: {text}")
        self._execute(["shell", "input", "text", formatted_text])

    def press_key(self, keycode: int):
        """Sends keycode events (e.g., Keycode 4 for BACK, 3 for HOME)."""
        logger.info(f"Injecting physical keycode event: {keycode}")
        self._execute(["shell", "input", "keyevent", str(keycode)])

    def start_app(self, package_name: str, activity_name: str):
        """Launches target app interface node."""
        logger.info(f"Launching Android Application: {package_name}/{activity_name}")
        self._execute(["shell", "am", "start", "-n", f"{package_name}/{activity_name}"])
