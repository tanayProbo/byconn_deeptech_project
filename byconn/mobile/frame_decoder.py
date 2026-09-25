import socket
import struct
import logging
from typing import Generator, Optional, Any

logger = logging.getLogger("byconnx.mobile.frame_decoder")

class ScrcpyFrameDecoder:
    """
    Decodes the raw H.264 video stream emitted by the Scrcpy on-device server.
    Establishes connection to the remote ADB forward socket and extracts raw frames.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = 27183):
        self.host = host
        self.port = port
        self.socket: Optional[socket.socket] = None

    def connect(self):
        """Establishes TCP connection to the forwarded scrcpy port."""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.connect((self.host, self.port))
            logger.info(f"Connected to scrcpy frame server socket on {self.host}:{self.port}")

            # Read scrcpy device metadata header
            # Format: device_name (64 bytes) + width (2 bytes) + height (2 bytes)
            metadata = self.socket.recv(68)
            if len(metadata) == 68:
                device_name = metadata[:64].decode('utf-8', errors='ignore').strip('\x00')
                width, height = struct.unpack(">HH", metadata[64:])
                logger.info(f"Attached device details: {device_name} ({width}x{height})")
        except Exception as e:
            logger.error(f"Failed to connect to scrcpy frame server: {str(e)}")

    def read_raw_h264_stream(self) -> Generator[bytes, None, None]:
        """
        Continuously reads packet frames off the TCP socket.
        Parses scrcpy's packet header (PTS timestamp [8 bytes] + packet size [4 bytes]).
        """
        if not self.socket:
            logger.error("Socket connection not initialized.")
            return

        while True:
            try:
                # Read 12-byte packet header
                header = self.socket.recv(12)
                if not header or len(header) < 12:
                    break

                pts, packet_size = struct.unpack(">Q I", header)

                # Fetch payload data bytes
                payload = b""
                while len(payload) < packet_size:
                    chunk = self.socket.recv(packet_size - len(payload))
                    if not chunk:
                        break
                    payload += chunk

                if len(payload) == packet_size:
                    yield payload
            except socket.error as e:
                logger.error(f"Socket connection read interrupted: {str(e)}")
                break

    def decode_frame(self, h264_payload: bytes) -> Optional[Any]:
        """
        Decodes h264 NAL units into raw image frames (numpy array format).
        Typically wraps around libraries like PyAV (libavcodec) or OpenCV.
        """
        # Skeleton demonstration of frame decoder parse hooks
        logger.debug(f"Passing raw H.264 payload ({len(h264_payload)} bytes) to video decoder backend...")
        # In a complete implementation, this invokes:
        # container.decode(packet) -> yields frame -> converted to RGB image
        return None

    def close(self):
        if self.socket:
            self.socket.close()
            logger.info("Scrcpy stream client disconnected.")
