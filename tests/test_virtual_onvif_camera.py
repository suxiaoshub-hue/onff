import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from virtual_onvif_camera import (
    CameraConfig,
    dispatch_soap,
    discovery_probe_match,
    extract_message_id,
)


def make_config(frame_dir: str) -> CameraConfig:
    return CameraConfig(
        Namespace(
            host="127.0.0.1",
            port=8080,
            public_host="192.0.2.10",
            rtsp_url="rtsp://192.0.2.20:8554/cam1",
            snapshot_url="",
            name="TestCam",
            manufacturer="Codex",
            model="Virtual ONVIF Camera",
            serial="TEST-001",
            hardware_id="HW-001",
            location="lab",
            uuid="11111111-1111-1111-1111-111111111111",
            frame_dir=frame_dir,
            frame_seconds=15,
            mjpeg_delay=0.25,
            no_discovery=True,
            discovery_addr="239.255.255.250",
            discovery_port=3702,
        )
    )


class VirtualOnvifCameraTests(unittest.TestCase):
    def test_get_stream_uri_uses_configured_rtsp_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "1.jpg").write_bytes(b"\xff\xd8\xff\xd9")
            config = make_config(tmp)
            response = dispatch_soap(config, "<trt:GetStreamUri/>").decode()

        self.assertIn("rtsp://192.0.2.20:8554/cam1", response)
        self.assertIn("GetStreamUriResponse", response)

    def test_get_capabilities_advertises_device_and_media_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            response = dispatch_soap(config, "<tds:GetCapabilities/>").decode()

        self.assertIn("http://192.0.2.10:8080/onvif/device_service", response)
        self.assertIn("http://192.0.2.10:8080/onvif/media_service", response)

    def test_discovery_probe_match_contains_uuid_and_xaddr(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(tmp)
            response = discovery_probe_match(config, "uuid:probe-message").decode()

        self.assertIn("urn:uuid:11111111-1111-1111-1111-111111111111", response)
        self.assertIn("http://192.0.2.10:8080/onvif/device_service", response)
        self.assertIn("<a:RelatesTo>uuid:probe-message</a:RelatesTo>", response)

    def test_extract_message_id(self):
        payload = "<s:Header><wsa:MessageID>uuid:abc</wsa:MessageID></s:Header>"

        self.assertEqual("uuid:abc", extract_message_id(payload))


if __name__ == "__main__":
    unittest.main()
