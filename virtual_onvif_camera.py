#!/usr/bin/env python3
"""
Lightweight virtual ONVIF camera.

The process exposes enough ONVIF Device/Media SOAP endpoints and WS-Discovery
responses for many NVR/VMS clients to discover it as a camera. Video is served
from a configured RTSP URL; snapshot and MJPEG preview can be served from local
JPEG frames bundled with the app.
"""

from __future__ import annotations

import argparse
import configparser
import copy
import datetime as _dt
import html
import ipaddress
import itertools
import mimetypes
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlparse


SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
WSA = "http://www.w3.org/2005/08/addressing"
WSA_LEGACY = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
WSD = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
TDS = "http://www.onvif.org/ver10/device/wsdl"
TRT = "http://www.onvif.org/ver10/media/wsdl"
TT = "http://www.onvif.org/ver10/schema"
CONFIG_FILE_NAME = "virtual_onvif_camera.ini"
SERVICE_NAME = "VirtualOnvifCamera"
SERVICE_DISPLAY_NAME = "Virtual ONVIF Camera"
SERVICE_DESCRIPTION = "Runs a virtual ONVIF camera gateway."
DISCOVERY_TO = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"
DISCOVERY_SCOPE_MATCH_BY = "http://schemas.xmlsoap.org/ws/2005/04/discovery/rfc3986"
DISCOVERY_INSTANCE_ID = int(time.time())
DISCOVERY_MESSAGE_NUMBER = itertools.count(1)


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def local_ip_for(target: str = "8.8.8.8") -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def is_usable_lan_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.version == 4 and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified


def local_ipv4_candidates() -> list[str]:
    candidates: list[str] = []

    def add(value: str) -> None:
        if is_usable_lan_ip(value) and value not in candidates:
            candidates.append(value)

    for target in ("239.255.255.250", "192.168.1.1", "192.168.0.1", "10.0.0.1", "172.16.0.1", "8.8.8.8"):
        add(local_ip_for(target))

    for host in {socket.gethostname(), socket.getfqdn()}:
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            continue
        for info in infos:
            add(info[4][0])

    def score(value: str) -> tuple[int, str]:
        ip = ipaddress.ip_address(value)
        return (0 if ip.is_private else 1, value)

    return sorted(candidates, key=score)


def auto_public_host() -> str:
    candidates = local_ipv4_candidates()
    return candidates[0] if candidates else "127.0.0.1"


def host_without_port(value: str) -> str:
    value = value.strip()
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def tcp_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def default_frame_dir() -> Path:
    root = app_root()
    return first_existing(
        [
            root / "static" / "cameras" / "1",
            root / "onvif_simulator" / "static" / "parkings" / "1",
            root / "onvif_simulator" / "static" / "cameras" / "1",
        ]
    ) or root


def default_config_path() -> Path:
    env_path = os.getenv("ONVIF_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return install_dir() / CONFIG_FILE_NAME


def default_config_text() -> str:
    return f"""# Virtual ONVIF Camera configuration
#
# Most users can leave this file unchanged. The default RTSP URL is
# rtsp://<this-computer-ip>:8554/VirtualCamera. If you already have a real
# RTSP source, put it in rtsp_url.

[camera]
host = 0.0.0.0
port = 8000
public_host = auto
rtsp_url = auto
rtsp_port = 8554
screen_stream = true
screen_fps = 15
screen_width = 1280
screen_height = 720
screen_bitrate = 6000k
capture_backend = auto
capture_output = 0
draw_mouse = true
encoder = auto
encoder_preset = veryfast
h264_profile = baseline
keyframe_seconds = 1
snapshot_url =
name = VirtualCamera
username = admin
password = admin
manufacturer = Codex
model = Virtual ONVIF Camera
serial = VOC-0001
hardware_id = VOC-HW-1
location = server
uuid =
frame_dir = auto
frame_seconds = 15
mjpeg_delay = 0.25
discovery = true
discovery_addr = 239.255.255.250
discovery_port = 3702
"""


def ensure_config_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(default_config_text(), encoding="utf-8")
    return path


def config_bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.strip().lower() in {"1", "yes", "true", "on"}


def read_config_defaults(path: Path) -> dict[str, str]:
    ensure_config_file(path)
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    section = parser["camera"] if parser.has_section("camera") else {}
    return {
        "host": section.get("host", "0.0.0.0"),
        "port": section.get("port", os.getenv("ONVIF_HTTP_PORT", "8000")),
        "public_host": section.get("public_host", os.getenv("ONVIF_PUBLIC_HOST", "auto")),
        "rtsp_url": section.get("rtsp_url", os.getenv("ONVIF_RTSP_URL", "auto")),
        "rtsp_port": section.get("rtsp_port", os.getenv("ONVIF_RTSP_PORT", "8554")),
        "screen_stream": section.get("screen_stream", os.getenv("ONVIF_SCREEN_STREAM", "true")),
        "screen_fps": section.get("screen_fps", os.getenv("ONVIF_SCREEN_FPS", "15")),
        "screen_width": section.get("screen_width", os.getenv("ONVIF_SCREEN_WIDTH", "1280")),
        "screen_height": section.get("screen_height", os.getenv("ONVIF_SCREEN_HEIGHT", "720")),
        "screen_bitrate": section.get("screen_bitrate", os.getenv("ONVIF_SCREEN_BITRATE", "6000k")),
        "capture_backend": section.get("capture_backend", os.getenv("ONVIF_CAPTURE_BACKEND", "auto")),
        "capture_output": section.get("capture_output", os.getenv("ONVIF_CAPTURE_OUTPUT", "0")),
        "draw_mouse": section.get("draw_mouse", os.getenv("ONVIF_DRAW_MOUSE", "true")),
        "encoder": section.get("encoder", os.getenv("ONVIF_ENCODER", "auto")),
        "encoder_preset": section.get("encoder_preset", os.getenv("ONVIF_ENCODER_PRESET", "veryfast")),
        "h264_profile": section.get("h264_profile", os.getenv("ONVIF_H264_PROFILE", "baseline")),
        "keyframe_seconds": section.get("keyframe_seconds", os.getenv("ONVIF_KEYFRAME_SECONDS", "1")),
        "snapshot_url": section.get("snapshot_url", os.getenv("ONVIF_SNAPSHOT_URL", "")),
        "name": section.get("name", os.getenv("ONVIF_NAME", "VirtualCamera")),
        "username": section.get("username", os.getenv("ONVIF_USERNAME", "admin")),
        "password": section.get("password", os.getenv("ONVIF_PASSWORD", "admin")),
        "manufacturer": section.get("manufacturer", os.getenv("ONVIF_MANUFACTURER", "Codex")),
        "model": section.get("model", os.getenv("ONVIF_MODEL", "Virtual ONVIF Camera")),
        "serial": section.get("serial", os.getenv("ONVIF_SERIAL", "VOC-0001")),
        "hardware_id": section.get("hardware_id", os.getenv("ONVIF_HARDWARE_ID", "VOC-HW-1")),
        "location": section.get("location", os.getenv("ONVIF_LOCATION", "server")),
        "uuid": section.get("uuid", os.getenv("ONVIF_UUID", "")),
        "frame_dir": section.get("frame_dir", os.getenv("ONVIF_FRAME_DIR", "auto")),
        "frame_seconds": section.get("frame_seconds", "15"),
        "mjpeg_delay": section.get("mjpeg_delay", "0.25"),
        "discovery": section.get("discovery", "true"),
        "discovery_addr": section.get("discovery_addr", "239.255.255.250"),
        "discovery_port": section.get("discovery_port", "3702"),
    }


def xml_escape(value: str) -> str:
    return html.escape(value, quote=True)


def iso_utc_now() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CameraConfig:
    def __init__(self, args: argparse.Namespace) -> None:
        self.host = args.host
        self.port = args.port
        self.name = args.name
        self.username = args.username
        self.password = args.password
        self.manufacturer = args.manufacturer
        self.model = args.model
        self.serial = args.serial
        self.hardware_id = args.hardware_id
        self.location = args.location
        self.uuid = normalize_uuid(args.uuid or None)
        self.rtsp_url = "" if args.rtsp_url == "auto" else args.rtsp_url
        self.rtsp_port = args.rtsp_port
        self.screen_stream_enabled = bool(args.screen_stream)
        self.screen_fps = max(1, args.screen_fps)
        self.screen_width = max(320, args.screen_width)
        self.screen_height = max(180, args.screen_height)
        self.screen_bitrate = args.screen_bitrate
        self.capture_backend = args.capture_backend.strip().lower()
        self.capture_output = max(0, args.capture_output)
        self.draw_mouse = bool(args.draw_mouse)
        self.encoder = args.encoder.strip().lower()
        self.encoder_preset = args.encoder_preset
        self.h264_profile = args.h264_profile
        self.keyframe_seconds = max(1, args.keyframe_seconds)
        self.snapshot_url = args.snapshot_url
        self.public_host = auto_public_host() if args.public_host in {None, "", "auto"} else args.public_host
        frame_dir = str(default_frame_dir()) if args.frame_dir == "auto" else args.frame_dir
        self.frame_dir = Path(frame_dir).expanduser().resolve()
        self.frame_seconds = max(1, args.frame_seconds)
        self.mjpeg_delay = max(0.05, args.mjpeg_delay)
        self.discovery_enabled = bool(getattr(args, "discovery_enabled", not getattr(args, "no_discovery", False)))
        self.discovery_addr = args.discovery_addr
        self.discovery_port = args.discovery_port

    @property
    def base_url(self) -> str:
        return f"http://{self.public_host}:{self.port}"

    @property
    def device_service_url(self) -> str:
        return f"{self.base_url}/onvif/device_service"

    @property
    def media_service_url(self) -> str:
        return f"{self.base_url}/onvif/media_service"

    @property
    def stream_path(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.~-]+", "_", self.name).strip("_")
        return safe or "VirtualCamera"

    @property
    def stream_uri(self) -> str:
        return self.rtsp_url or f"rtsp://{self.public_host}:{self.rtsp_port}/{quote(self.stream_path)}"

    @property
    def local_publish_uri(self) -> str:
        return f"rtsp://127.0.0.1:{self.rtsp_port}/{quote(self.stream_path)}"

    @property
    def should_start_screen_stream(self) -> bool:
        return self.screen_stream_enabled and not self.rtsp_url

    @property
    def effective_snapshot_url(self) -> str:
        return self.snapshot_url or f"{self.base_url}/snapshot.jpg"

    def frames(self) -> list[Path]:
        frames = sorted(
            p
            for p in self.frame_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg"}
        )
        return frames

    def current_frame(self) -> Path | None:
        frames = self.frames()
        if not frames:
            return None
        index = int(time.time() / self.frame_seconds) % len(frames)
        return frames[index]


def normalize_uuid(value: str | None) -> str:
    if value:
        value = value.strip()
        if value.startswith("urn:uuid:"):
            value = value.removeprefix("urn:uuid:")
        return str(uuid.UUID(value))
    return str(uuid.uuid4())


def soap_envelope(body: str, action: str | None = None) -> bytes:
    action_node = (
        f"<wsa:Action>{xml_escape(action)}</wsa:Action>"
        if action
        else ""
    )
    message_id = f"urn:uuid:{uuid.uuid4()}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_ENV}" xmlns:wsa="{WSA}" xmlns:tds="{TDS}" xmlns:trt="{TRT}" xmlns:tt="{TT}">
  <s:Header>
    {action_node}
    <wsa:MessageID>{message_id}</wsa:MessageID>
    <wsa:To s:mustUnderstand="true">{WSA}/anonymous</wsa:To>
  </s:Header>
  <s:Body>
    {body}
  </s:Body>
</s:Envelope>
"""
    return xml.encode("utf-8")


def fault(reason: str) -> bytes:
    return soap_envelope(
        f"""<s:Fault xmlns:s="{SOAP_ENV}">
      <s:Code><s:Value>s:Sender</s:Value></s:Code>
      <s:Reason><s:Text xml:lang="en">{xml_escape(reason)}</s:Text></s:Reason>
    </s:Fault>"""
    )


def get_services(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"""<tds:GetServicesResponse>
      <tds:Service>
        <tds:Namespace>{TDS}</tds:Namespace>
        <tds:XAddr>{xml_escape(config.device_service_url)}</tds:XAddr>
        <tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service>
        <tds:Namespace>{TRT}</tds:Namespace>
        <tds:XAddr>{xml_escape(config.media_service_url)}</tds:XAddr>
        <tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version>
      </tds:Service>
    </tds:GetServicesResponse>""",
        f"{TDS}/GetServicesResponse",
    )


def get_device_service_capabilities() -> bytes:
    return soap_envelope(
        """<tds:GetServiceCapabilitiesResponse>
      <tds:Capabilities>
        <tds:Network IPFilter="false" ZeroConfiguration="false" IPVersion6="false" DynDNS="false"/>
        <tds:Security TLS1.1="false" TLS1.2="false" OnboardKeyGeneration="false" AccessPolicyConfig="false" X.509Token="false" SAMLToken="false" KerberosToken="false" RELToken="false" UsernameToken="true" HttpDigest="false"/>
        <tds:System DiscoveryResolve="true" DiscoveryBye="true" RemoteDiscovery="false" SystemBackup="false" SystemLogging="false" FirmwareUpgrade="false"/>
      </tds:Capabilities>
    </tds:GetServiceCapabilitiesResponse>""",
        f"{TDS}/GetServiceCapabilitiesResponse",
    )


def get_media_service_capabilities() -> bytes:
    return soap_envelope(
        """<trt:GetServiceCapabilitiesResponse>
      <trt:Capabilities SnapshotUri="true" Rotation="false" VideoSourceMode="false" OSD="false">
        <trt:ProfileCapabilities MaximumNumberOfProfiles="1"/>
        <trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true" NonAggregateControl="false"/>
      </trt:Capabilities>
    </trt:GetServiceCapabilitiesResponse>""",
        f"{TRT}/GetServiceCapabilitiesResponse",
    )


def get_capabilities(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"""<tds:GetCapabilitiesResponse>
      <tds:Capabilities>
        <tt:Device>
          <tt:XAddr>{xml_escape(config.device_service_url)}</tt:XAddr>
          <tt:Network><tt:IPFilter>false</tt:IPFilter><tt:ZeroConfiguration>false</tt:ZeroConfiguration><tt:IPVersion6>false</tt:IPVersion6><tt:DynDNS>false</tt:DynDNS></tt:Network>
          <tt:System><tt:DiscoveryResolve>true</tt:DiscoveryResolve><tt:DiscoveryBye>true</tt:DiscoveryBye><tt:RemoteDiscovery>false</tt:RemoteDiscovery><tt:SystemBackup>false</tt:SystemBackup><tt:SystemLogging>false</tt:SystemLogging><tt:FirmwareUpgrade>false</tt:FirmwareUpgrade></tt:System>
        </tt:Device>
        <tt:Media>
          <tt:XAddr>{xml_escape(config.media_service_url)}</tt:XAddr>
          <tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities>
        </tt:Media>
      </tds:Capabilities>
    </tds:GetCapabilitiesResponse>""",
        f"{TDS}/GetCapabilitiesResponse",
    )


def get_endpoint_reference(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"""<tds:GetEndpointReferenceResponse>
      <tds:GUID>urn:uuid:{xml_escape(config.uuid)}</tds:GUID>
    </tds:GetEndpointReferenceResponse>""",
        f"{TDS}/GetEndpointReferenceResponse",
    )


def get_device_information(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"""<tds:GetDeviceInformationResponse>
      <tds:Manufacturer>{xml_escape(config.manufacturer)}</tds:Manufacturer>
      <tds:Model>{xml_escape(config.model)}</tds:Model>
      <tds:FirmwareVersion>1.0.0</tds:FirmwareVersion>
      <tds:SerialNumber>{xml_escape(config.serial)}</tds:SerialNumber>
      <tds:HardwareId>{xml_escape(config.hardware_id)}</tds:HardwareId>
    </tds:GetDeviceInformationResponse>""",
        f"{TDS}/GetDeviceInformationResponse",
    )


def get_hostname(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"""<tds:GetHostnameResponse>
      <tds:HostnameInformation>
        <tt:FromDHCP>false</tt:FromDHCP>
        <tt:Name>{xml_escape(config.name)}</tt:Name>
      </tds:HostnameInformation>
    </tds:GetHostnameResponse>""",
        f"{TDS}/GetHostnameResponse",
    )


def get_network_interfaces(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"""<tds:GetNetworkInterfacesResponse>
      <tds:NetworkInterfaces token="eth0">
        <tt:Enabled>true</tt:Enabled>
        <tt:Info><tt:Name>eth0</tt:Name><tt:HwAddress>02:00:00:00:00:01</tt:HwAddress><tt:MTU>1500</tt:MTU></tt:Info>
        <tt:IPv4>
          <tt:Enabled>true</tt:Enabled>
          <tt:Config>
            <tt:Manual><tt:Address>{xml_escape(config.public_host)}</tt:Address><tt:PrefixLength>24</tt:PrefixLength></tt:Manual>
            <tt:DHCP>false</tt:DHCP>
          </tt:Config>
        </tt:IPv4>
      </tds:NetworkInterfaces>
    </tds:GetNetworkInterfacesResponse>""",
        f"{TDS}/GetNetworkInterfacesResponse",
    )


def get_network_protocols(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"""<tds:GetNetworkProtocolsResponse>
      <tds:NetworkProtocols><tt:Name>HTTP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{config.port}</tt:Port></tds:NetworkProtocols>
      <tds:NetworkProtocols><tt:Name>RTSP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{config.rtsp_port}</tt:Port></tds:NetworkProtocols>
    </tds:GetNetworkProtocolsResponse>""",
        f"{TDS}/GetNetworkProtocolsResponse",
    )


def get_discovery_mode() -> bytes:
    return soap_envelope(
        """<tds:GetDiscoveryModeResponse>
      <tds:DiscoveryMode>Discoverable</tds:DiscoveryMode>
    </tds:GetDiscoveryModeResponse>""",
        f"{TDS}/GetDiscoveryModeResponse",
    )


def get_users(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"""<tds:GetUsersResponse>
      <tds:User>
        <tt:Username>{xml_escape(config.username)}</tt:Username>
        <tt:UserLevel>Administrator</tt:UserLevel>
      </tds:User>
    </tds:GetUsersResponse>""",
        f"{TDS}/GetUsersResponse",
    )


def get_scopes(config: CameraConfig) -> bytes:
    scopes = [
        "onvif://www.onvif.org/type/video_encoder",
        "onvif://www.onvif.org/type/Network_Video_Transmitter",
        f"onvif://www.onvif.org/name/{config.name}",
        f"onvif://www.onvif.org/location/{config.location}",
        f"onvif://www.onvif.org/hardware/{config.model}",
    ]
    scope_nodes = "\n".join(
        f"<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{xml_escape(scope)}</tt:ScopeItem></tds:Scopes>"
        for scope in scopes
    )
    return soap_envelope(
        f"<tds:GetScopesResponse>{scope_nodes}</tds:GetScopesResponse>",
        f"{TDS}/GetScopesResponse",
    )


def get_system_date_and_time() -> bytes:
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    return soap_envelope(
        f"""<tds:GetSystemDateAndTimeResponse>
      <tds:SystemDateAndTime>
        <tt:DateTimeType>NTP</tt:DateTimeType>
        <tt:DaylightSavings>false</tt:DaylightSavings>
        <tt:TimeZone><tt:TZ>UTC</tt:TZ></tt:TimeZone>
        <tt:UTCDateTime>
          <tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>
          <tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date>
        </tt:UTCDateTime>
      </tds:SystemDateAndTime>
    </tds:GetSystemDateAndTimeResponse>""",
        f"{TDS}/GetSystemDateAndTimeResponse",
    )


def video_dimensions(config: CameraConfig) -> tuple[int, int]:
    width = config.screen_width
    height = config.screen_height
    if width % 2:
        width += 1
    if height % 2:
        height += 1
    return width, height


def bitrate_kbps(config: CameraConfig) -> int:
    match = re.match(r"^\s*(\d+)\s*([kKmM]?)", config.screen_bitrate)
    if not match:
        return 6000
    value = int(match.group(1))
    suffix = match.group(2).lower()
    return value * 1000 if suffix == "m" else value


def bitrate_scaled(value: str, factor: int) -> str:
    match = re.match(r"^\s*(\d+)\s*([kKmM]?)\s*$", value)
    if not match:
        return value
    return f"{int(match.group(1)) * factor}{match.group(2)}"


def h264_profile_name(config: CameraConfig) -> str:
    value = config.h264_profile.strip().lower()
    if value == "baseline":
        return "Baseline"
    if value == "high":
        return "High"
    return "Main"


def h264_profile_value(config: CameraConfig) -> str:
    return h264_profile_name(config).lower()


def keyframe_interval(config: CameraConfig) -> int:
    return max(2, config.screen_fps * config.keyframe_seconds)


def profile_xml(config: CameraConfig) -> str:
    width, height = video_dimensions(config)
    bitrate = bitrate_kbps(config)
    return f"""<trt:Profiles fixed="true" token="profile_1">
      <tt:Name>{xml_escape(config.name)}</tt:Name>
      <tt:VideoSourceConfiguration token="video_source_config_1">
        <tt:Name>VideoSourceConfig</tt:Name>
        <tt:UseCount>1</tt:UseCount>
        <tt:SourceToken>video_source_1</tt:SourceToken>
        <tt:Bounds x="0" y="0" width="{width}" height="{height}"/>
      </tt:VideoSourceConfiguration>
      <tt:VideoEncoderConfiguration token="video_encoder_config_1">
        <tt:Name>H264</tt:Name>
        <tt:UseCount>1</tt:UseCount>
        <tt:Encoding>H264</tt:Encoding>
        <tt:Resolution><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height></tt:Resolution>
        <tt:Quality>5</tt:Quality>
        <tt:RateControl><tt:FrameRateLimit>{config.screen_fps}</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>{bitrate}</tt:BitrateLimit></tt:RateControl>
        <tt:H264><tt:GovLength>{keyframe_interval(config)}</tt:GovLength><tt:H264Profile>{h264_profile_name(config)}</tt:H264Profile></tt:H264>
        <tt:SessionTimeout>PT60S</tt:SessionTimeout>
      </tt:VideoEncoderConfiguration>
    </trt:Profiles>"""


def get_profiles(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"<trt:GetProfilesResponse>{profile_xml(config)}</trt:GetProfilesResponse>",
        f"{TRT}/GetProfilesResponse",
    )


def get_profile(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"<trt:GetProfileResponse>{profile_xml(config)}</trt:GetProfileResponse>",
        f"{TRT}/GetProfileResponse",
    )


def get_video_sources(config: CameraConfig) -> bytes:
    width, height = video_dimensions(config)
    return soap_envelope(
        f"""<trt:GetVideoSourcesResponse>
      <trt:VideoSources token="video_source_1">
        <tt:Framerate>{config.screen_fps}</tt:Framerate>
        <tt:Resolution><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height></tt:Resolution>
        <tt:Imaging><tt:Brightness>50</tt:Brightness><tt:ColorSaturation>50</tt:ColorSaturation><tt:Contrast>50</tt:Contrast><tt:Sharpness>50</tt:Sharpness></tt:Imaging>
      </trt:VideoSources>
    </trt:GetVideoSourcesResponse>""",
        f"{TRT}/GetVideoSourcesResponse",
    )


def video_source_configuration_xml(config: CameraConfig) -> str:
    width, height = video_dimensions(config)
    return f"""<trt:Configurations token="video_source_config_1">
      <tt:Name>VideoSourceConfig</tt:Name>
      <tt:UseCount>1</tt:UseCount>
      <tt:SourceToken>video_source_1</tt:SourceToken>
      <tt:Bounds x="0" y="0" width="{width}" height="{height}"/>
    </trt:Configurations>"""


def get_video_source_configurations(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"<trt:GetVideoSourceConfigurationsResponse>{video_source_configuration_xml(config)}</trt:GetVideoSourceConfigurationsResponse>",
        f"{TRT}/GetVideoSourceConfigurationsResponse",
    )


def get_video_source_configuration(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"<trt:GetVideoSourceConfigurationResponse>{video_source_configuration_xml(config)}</trt:GetVideoSourceConfigurationResponse>",
        f"{TRT}/GetVideoSourceConfigurationResponse",
    )


def get_compatible_video_encoder_configurations(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"<trt:GetCompatibleVideoEncoderConfigurationsResponse>{video_encoder_configuration_xml(config)}</trt:GetCompatibleVideoEncoderConfigurationsResponse>",
        f"{TRT}/GetCompatibleVideoEncoderConfigurationsResponse",
    )


def get_compatible_video_source_configurations(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"<trt:GetCompatibleVideoSourceConfigurationsResponse>{video_source_configuration_xml(config)}</trt:GetCompatibleVideoSourceConfigurationsResponse>",
        f"{TRT}/GetCompatibleVideoSourceConfigurationsResponse",
    )


def video_encoder_configuration_xml(config: CameraConfig) -> str:
    width, height = video_dimensions(config)
    bitrate = bitrate_kbps(config)
    return f"""<trt:Configurations token="video_encoder_config_1">
      <tt:Name>H264</tt:Name>
      <tt:UseCount>1</tt:UseCount>
      <tt:Encoding>H264</tt:Encoding>
      <tt:Resolution><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height></tt:Resolution>
      <tt:Quality>5</tt:Quality>
      <tt:RateControl><tt:FrameRateLimit>{config.screen_fps}</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>{bitrate}</tt:BitrateLimit></tt:RateControl>
      <tt:H264><tt:GovLength>{keyframe_interval(config)}</tt:GovLength><tt:H264Profile>{h264_profile_name(config)}</tt:H264Profile></tt:H264>
      <tt:SessionTimeout>PT60S</tt:SessionTimeout>
    </trt:Configurations>"""


def get_video_encoder_configurations(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"<trt:GetVideoEncoderConfigurationsResponse>{video_encoder_configuration_xml(config)}</trt:GetVideoEncoderConfigurationsResponse>",
        f"{TRT}/GetVideoEncoderConfigurationsResponse",
    )


def get_video_encoder_configuration(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"<trt:GetVideoEncoderConfigurationResponse>{video_encoder_configuration_xml(config)}</trt:GetVideoEncoderConfigurationResponse>",
        f"{TRT}/GetVideoEncoderConfigurationResponse",
    )


def get_video_encoder_configuration_options(config: CameraConfig) -> bytes:
    width, height = video_dimensions(config)
    resolutions = [(width, height), (1920, 1080), (1280, 720), (960, 540), (640, 360)]
    seen: set[tuple[int, int]] = set()
    resolution_nodes = []
    for item_width, item_height in resolutions:
        pair = (item_width, item_height)
        if pair in seen:
            continue
        seen.add(pair)
        resolution_nodes.append(
            f"<tt:ResolutionsAvailable><tt:Width>{item_width}</tt:Width><tt:Height>{item_height}</tt:Height></tt:ResolutionsAvailable>"
        )
    profile_nodes = "\n          ".join(
        f"<tt:H264ProfilesSupported>{profile}</tt:H264ProfilesSupported>"
        for profile in ("Baseline", "Main", "High")
    )
    return soap_envelope(
        f"""<trt:GetVideoEncoderConfigurationOptionsResponse>
      <trt:Options>
        <tt:QualityRange><tt:Min>1</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>
        <tt:H264>
          {"".join(resolution_nodes)}
          <tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>120</tt:Max></tt:GovLengthRange>
          <tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>60</tt:Max></tt:FrameRateRange>
          <tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>
          {profile_nodes}
        </tt:H264>
      </trt:Options>
    </trt:GetVideoEncoderConfigurationOptionsResponse>""",
        f"{TRT}/GetVideoEncoderConfigurationOptionsResponse",
    )


def empty_media_list_response(action: str) -> bytes:
    return soap_envelope(f"<trt:{action}Response/>", f"{TRT}/{action}Response")


def get_snapshot_uri(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"""<trt:GetSnapshotUriResponse>
      <trt:MediaUri>
        <tt:Uri>{xml_escape(config.effective_snapshot_url)}</tt:Uri>
        <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
        <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
        <tt:Timeout>PT60S</tt:Timeout>
      </trt:MediaUri>
    </trt:GetSnapshotUriResponse>""",
        f"{TRT}/GetSnapshotUriResponse",
    )


def get_stream_uri(config: CameraConfig) -> bytes:
    parsed = urlparse(config.stream_uri)
    if parsed.hostname and parsed.port and not config.rtsp_url:
        if tcp_port_open("127.0.0.1", parsed.port, timeout=0.3):
            print(f"[rtsp] stream uri requested and local RTSP port is open: {config.stream_uri}")
        else:
            print(f"[error] stream uri requested but local RTSP port is closed: 127.0.0.1:{parsed.port}")
    return soap_envelope(
        f"""<trt:GetStreamUriResponse>
      <trt:MediaUri>
        <tt:Uri>{xml_escape(config.stream_uri)}</tt:Uri>
        <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
        <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
        <tt:Timeout>PT60S</tt:Timeout>
      </trt:MediaUri>
    </trt:GetStreamUriResponse>""",
        f"{TRT}/GetStreamUriResponse",
    )


def detect_soap_action(payload: str) -> str:
    for action in [
        "GetCompatibleVideoEncoderConfigurations",
        "GetCompatibleVideoSourceConfigurations",
        "GetVideoEncoderConfigurationOptions",
        "GetVideoEncoderConfigurations",
        "GetVideoEncoderConfiguration",
        "GetVideoSourceConfigurations",
        "GetVideoSourceConfiguration",
        "GetServiceCapabilities",
        "GetEndpointReference",
        "GetNetworkInterfaces",
        "GetNetworkProtocols",
        "GetDeviceInformation",
        "GetSystemDateAndTime",
        "GetDiscoveryMode",
        "GetCapabilities",
        "GetSnapshotUri",
        "GetStreamUri",
        "GetVideoSources",
        "GetMetadataConfigurations",
        "GetMetadataConfiguration",
        "GetAudioSourceConfigurations",
        "GetAudioEncoderConfigurations",
        "GetAudioSources",
        "GetAudioOutputs",
        "GetAudioDecoderConfigurations",
        "GetAudioOutputConfigurations",
        "GetOSDs",
        "GetProfiles",
        "GetProfile",
        "GetServices",
        "GetHostname",
        "GetScopes",
        "GetUsers",
        "SetSynchronizationPoint",
        "SetSystemDateAndTime",
    ]:
        if action in payload:
            return action
    return "Unsupported"


def summarize_soap_action(payload: str) -> str:
    action_match = re.search(r"<[^:>]*:?Action[^>]*>([^<]+)</[^:>]*:?Action>", payload)
    if action_match:
        return action_match.group(1).strip().rsplit("/", 1)[-1]
    body_match = re.search(r"<(?:\w+:)?Body[^>]*>\s*<([^\s>/]+)", payload)
    if body_match:
        return body_match.group(1).split(":", 1)[-1]
    return "Unknown"


def dispatch_soap(config: CameraConfig, payload: str) -> bytes:
    action = detect_soap_action(payload)
    if action == "GetServices":
        return get_services(config)
    if action == "GetServiceCapabilities":
        if "trt:GetServiceCapabilities" in payload or f"{TRT}/GetServiceCapabilities" in payload:
            return get_media_service_capabilities()
        return get_device_service_capabilities()
    if action == "GetCapabilities":
        return get_capabilities(config)
    if action == "GetEndpointReference":
        return get_endpoint_reference(config)
    if action == "GetDeviceInformation":
        return get_device_information(config)
    if action == "GetHostname":
        return get_hostname(config)
    if action == "GetNetworkInterfaces":
        return get_network_interfaces(config)
    if action == "GetNetworkProtocols":
        return get_network_protocols(config)
    if action == "GetDiscoveryMode":
        return get_discovery_mode()
    if action == "GetUsers":
        return get_users(config)
    if action == "GetScopes":
        return get_scopes(config)
    if action == "GetSystemDateAndTime":
        return get_system_date_and_time()
    if action == "GetProfiles":
        return get_profiles(config)
    if action == "GetProfile":
        return get_profile(config)
    if action == "GetVideoSources":
        return get_video_sources(config)
    if action == "GetVideoSourceConfigurations":
        return get_video_source_configurations(config)
    if action == "GetVideoSourceConfiguration":
        return get_video_source_configuration(config)
    if action == "GetCompatibleVideoSourceConfigurations":
        return get_compatible_video_source_configurations(config)
    if action == "GetVideoEncoderConfigurations":
        return get_video_encoder_configurations(config)
    if action == "GetVideoEncoderConfiguration":
        return get_video_encoder_configuration(config)
    if action == "GetCompatibleVideoEncoderConfigurations":
        return get_compatible_video_encoder_configurations(config)
    if action == "GetVideoEncoderConfigurationOptions":
        return get_video_encoder_configuration_options(config)
    if action in {
        "GetMetadataConfigurations",
        "GetMetadataConfiguration",
        "GetAudioSourceConfigurations",
        "GetAudioEncoderConfigurations",
        "GetAudioSources",
        "GetAudioOutputs",
        "GetAudioDecoderConfigurations",
        "GetAudioOutputConfigurations",
        "GetOSDs",
    }:
        return empty_media_list_response(action)
    if action == "SetSynchronizationPoint":
        return soap_envelope("<trt:SetSynchronizationPointResponse/>", f"{TRT}/SetSynchronizationPointResponse")
    if action == "SetSystemDateAndTime":
        return soap_envelope("<tds:SetSystemDateAndTimeResponse/>", f"{TDS}/SetSystemDateAndTimeResponse")
    if action == "GetSnapshotUri":
        return get_snapshot_uri(config)
    if action == "GetStreamUri":
        return get_stream_uri(config)
    return fault("Unsupported ONVIF action")


class VirtualCameraHandler(BaseHTTPRequestHandler):
    server_version = "VirtualONVIFCamera/1.0"

    @property
    def config(self) -> CameraConfig:
        return self.server.config  # type: ignore[attr-defined]

    def config_for_request(self) -> CameraConfig:
        config = copy.copy(self.config)
        host = host_without_port(self.headers.get("Host", ""))
        if host and host not in {"0.0.0.0", "::"}:
            config.public_host = host
            return config
        local_host = self.connection.getsockname()[0]
        if is_usable_lan_ip(local_host):
            config.public_host = local_host
        return config

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[http] {self.client_address[0]} - {fmt % args}")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self.write_status_page()
        elif path in {"/snapshot.jpg", "/snapshot/1.jpg", "/camera/1/"}:
            self.write_snapshot()
        elif path in {"/mjpeg", "/mjpeg/1"}:
            self.write_mjpeg()
        elif path.startswith("/static/"):
            self.write_static(path.removeprefix("/static/"))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/onvif"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(size).decode("utf-8", errors="replace")
        action = detect_soap_action(payload)
        requested_action = summarize_soap_action(payload)
        print(f"[soap] {self.client_address[0]} -> {action} (requested: {requested_action})")
        if action == "Unsupported":
            preview = " ".join(payload.split())[:500]
            print(f"[soap] unsupported payload preview: {preview}")
        response = dispatch_soap(self.config_for_request(), payload)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def write_status_page(self) -> None:
        body = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>{xml_escape(self.config.name)}</title>
<body>
<h1>{xml_escape(self.config.name)}</h1>
<p>ONVIF Device Service: <code>{xml_escape(self.config.device_service_url)}</code></p>
<p>ONVIF Media Service: <code>{xml_escape(self.config.media_service_url)}</code></p>
<p>RTSP Stream URI: <code>{xml_escape(self.config.stream_uri)}</code></p>
<p>Snapshot: <a href="/snapshot.jpg">/snapshot.jpg</a></p>
<p>MJPEG preview: <a href="/mjpeg/1">/mjpeg/1</a></p>
</body>
</html>
""".encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_snapshot(self) -> None:
        frame = self.config.current_frame()
        if frame is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No JPEG frames found")
            return
        data = frame.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def write_mjpeg(self) -> None:
        frames = self.config.frames()
        if not frames:
            self.send_error(HTTPStatus.NOT_FOUND, "No JPEG frames found")
            return
        boundary = "virtual-onvif-camera"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        for frame in itertools.cycle(frames):
            try:
                data = frame.read_bytes()
                self.wfile.write(f"--{boundary}\r\n".encode("ascii"))
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(self.config.mjpeg_delay)
            except (BrokenPipeError, ConnectionResetError, OSError):
                break

    def write_static(self, relative: str) -> None:
        root = app_root() / "static"
        target = (root / relative).resolve()
        if not str(target).startswith(str(root.resolve())) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ConfiguredHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], handler, config: CameraConfig) -> None:
        self.config = config
        super().__init__(addr, handler)


def discovery_scopes(config: CameraConfig) -> str:
    return " ".join(
        [
            "onvif://www.onvif.org/type/video_encoder",
            "onvif://www.onvif.org/type/Network_Video_Transmitter",
            f"onvif://www.onvif.org/name/{config.name}",
            f"onvif://www.onvif.org/location/{config.location}",
        ]
    )


def discovery_types() -> str:
    return "dn:NetworkVideoTransmitter tds:Device"


def discovery_app_sequence() -> str:
    return f'<d:AppSequence InstanceId="{DISCOVERY_INSTANCE_ID}" MessageNumber="{next(DISCOVERY_MESSAGE_NUMBER)}"/>'


def discovery_device_service_url(config: CameraConfig, public_host: str | None = None) -> str:
    if public_host:
        return f"http://{public_host}:{config.port}/onvif/device_service"
    return config.device_service_url


def discovery_target_xml(config: CameraConfig, tag: str, public_host: str | None = None) -> str:
    scopes = discovery_scopes(config)
    xaddr = discovery_device_service_url(config, public_host)
    return f"""      <d:{tag}>
        <a:EndpointReference>
          <a:Address>urn:uuid:{xml_escape(config.uuid)}</a:Address>
        </a:EndpointReference>
        <d:Types>{discovery_types()}</d:Types>
        <d:Scopes MatchBy="{DISCOVERY_SCOPE_MATCH_BY}">{xml_escape(scopes)}</d:Scopes>
        <d:XAddrs>{xml_escape(xaddr)}</d:XAddrs>
        <d:MetadataVersion>1</d:MetadataVersion>
      </d:{tag}>"""


def discovery_probe_match(config: CameraConfig, relates_to: str | None = None, public_host: str | None = None) -> bytes:
    relates = f"<a:RelatesTo>{xml_escape(relates_to)}</a:RelatesTo>" if relates_to else ""
    message_id = f"urn:uuid:{uuid.uuid4()}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="{SOAP_ENV}" xmlns:a="{WSA}" xmlns:d="{WSD}" xmlns:dn="http://www.onvif.org/ver10/network/wsdl" xmlns:tds="{TDS}">
  <e:Header>
    <a:Action>{WSD}/ProbeMatches</a:Action>
    <a:MessageID>{message_id}</a:MessageID>
    {relates}
    <a:To>{WSA}/anonymous</a:To>
    {discovery_app_sequence()}
  </e:Header>
  <e:Body>
    <d:ProbeMatches>
{discovery_target_xml(config, "ProbeMatch", public_host)}
    </d:ProbeMatches>
  </e:Body>
</e:Envelope>
"""
    return xml.encode("utf-8")


def legacy_discovery_probe_match(config: CameraConfig, relates_to: str | None = None, public_host: str | None = None) -> bytes:
    relates = f"<wsa:RelatesTo>{xml_escape(relates_to)}</wsa:RelatesTo>" if relates_to else ""
    scopes = discovery_scopes(config)
    xaddr = discovery_device_service_url(config, public_host)
    message_id = f"uuid:{uuid.uuid4()}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="{SOAP_ENV}" xmlns:wsa="{WSA_LEGACY}" xmlns:wsdd="{WSD}" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <SOAP-ENV:Header>
    <wsa:MessageID>{message_id}</wsa:MessageID>
    <wsa:To>{WSA_LEGACY}/role/anonymous</wsa:To>
    <wsa:Action>{WSD}/ProbeMatches</wsa:Action>
    {relates}
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <wsdd:ProbeMatches>
      <wsdd:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:{xml_escape(config.uuid)}</wsa:Address>
        </wsa:EndpointReference>
        <wsdd:Types>dn:NetworkVideoTransmitter</wsdd:Types>
        <wsdd:Scopes>{xml_escape(scopes)}</wsdd:Scopes>
        <wsdd:XAddrs>{xml_escape(xaddr)}</wsdd:XAddrs>
        <wsdd:MetadataVersion>1</wsdd:MetadataVersion>
      </wsdd:ProbeMatch>
    </wsdd:ProbeMatches>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""
    return xml.encode("utf-8")


def discovery_resolve_match(config: CameraConfig, relates_to: str | None = None, public_host: str | None = None) -> bytes:
    relates = f"<a:RelatesTo>{xml_escape(relates_to)}</a:RelatesTo>" if relates_to else ""
    message_id = f"urn:uuid:{uuid.uuid4()}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="{SOAP_ENV}" xmlns:a="{WSA}" xmlns:d="{WSD}" xmlns:dn="http://www.onvif.org/ver10/network/wsdl" xmlns:tds="{TDS}">
  <e:Header>
    <a:Action>{WSD}/ResolveMatches</a:Action>
    <a:MessageID>{message_id}</a:MessageID>
    {relates}
    <a:To>{WSA}/anonymous</a:To>
    {discovery_app_sequence()}
  </e:Header>
  <e:Body>
    <d:ResolveMatches>
{discovery_target_xml(config, "ResolveMatch", public_host)}
    </d:ResolveMatches>
  </e:Body>
</e:Envelope>
"""
    return xml.encode("utf-8")


def legacy_discovery_resolve_match(config: CameraConfig, relates_to: str | None = None, public_host: str | None = None) -> bytes:
    relates = f"<wsa:RelatesTo>{xml_escape(relates_to)}</wsa:RelatesTo>" if relates_to else ""
    scopes = discovery_scopes(config)
    xaddr = discovery_device_service_url(config, public_host)
    message_id = f"uuid:{uuid.uuid4()}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="{SOAP_ENV}" xmlns:wsa="{WSA_LEGACY}" xmlns:wsdd="{WSD}" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <SOAP-ENV:Header>
    <wsa:MessageID>{message_id}</wsa:MessageID>
    <wsa:To>{WSA_LEGACY}/role/anonymous</wsa:To>
    <wsa:Action>{WSD}/ResolveMatches</wsa:Action>
    {relates}
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <wsdd:ResolveMatches>
      <wsdd:ResolveMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:{xml_escape(config.uuid)}</wsa:Address>
        </wsa:EndpointReference>
        <wsdd:Types>dn:NetworkVideoTransmitter</wsdd:Types>
        <wsdd:Scopes>{xml_escape(scopes)}</wsdd:Scopes>
        <wsdd:XAddrs>{xml_escape(xaddr)}</wsdd:XAddrs>
        <wsdd:MetadataVersion>1</wsdd:MetadataVersion>
      </wsdd:ResolveMatch>
    </wsdd:ResolveMatches>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""
    return xml.encode("utf-8")


def discovery_hello(config: CameraConfig) -> bytes:
    message_id = f"urn:uuid:{uuid.uuid4()}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="{SOAP_ENV}" xmlns:a="{WSA}" xmlns:d="{WSD}" xmlns:dn="http://www.onvif.org/ver10/network/wsdl" xmlns:tds="{TDS}">
  <e:Header>
    <a:Action>{WSD}/Hello</a:Action>
    <a:MessageID>{message_id}</a:MessageID>
    <a:To>{DISCOVERY_TO}</a:To>
    {discovery_app_sequence()}
  </e:Header>
  <e:Body>
{discovery_target_xml(config, "Hello")}
  </e:Body>
</e:Envelope>
"""
    return xml.encode("utf-8")


def legacy_discovery_hello(config: CameraConfig) -> bytes:
    scopes = discovery_scopes(config)
    message_id = f"uuid:{uuid.uuid4()}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="{SOAP_ENV}" xmlns:wsa="{WSA_LEGACY}" xmlns:wsdd="{WSD}" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <SOAP-ENV:Header>
    <wsa:MessageID>{message_id}</wsa:MessageID>
    <wsa:To>{DISCOVERY_TO}</wsa:To>
    <wsa:Action>{WSD}/Hello</wsa:Action>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <wsdd:Hello>
      <wsa:EndpointReference>
        <wsa:Address>urn:uuid:{xml_escape(config.uuid)}</wsa:Address>
      </wsa:EndpointReference>
      <wsdd:Types>dn:NetworkVideoTransmitter</wsdd:Types>
      <wsdd:Scopes>{xml_escape(scopes)}</wsdd:Scopes>
      <wsdd:XAddrs>{xml_escape(config.device_service_url)}</wsdd:XAddrs>
      <wsdd:MetadataVersion>1</wsdd:MetadataVersion>
    </wsdd:Hello>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""
    return xml.encode("utf-8")


def extract_message_id(payload: str) -> str | None:
    for tag in ("MessageID", "wsa:MessageID", "a:MessageID"):
        start = payload.find(f"<{tag}>")
        end = payload.find(f"</{tag}>")
        if start >= 0 and end > start:
            return payload[start + len(tag) + 2 : end].strip()
    return None


class DiscoveryServer(threading.Thread):
    def __init__(self, config: CameraConfig) -> None:
        super().__init__(daemon=True)
        self.config = config
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock:
            self._sock.close()

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock = sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            if is_usable_lan_ip(self.config.public_host):
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.config.public_host))
        except OSError:
            pass
        try:
            sock.bind(("", self.config.discovery_port))
            mreq = socket.inet_aton(self.config.discovery_addr) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            print(f"[discovery] listening on udp://{self.config.discovery_addr}:{self.config.discovery_port}")
        except OSError as exc:
            print(f"[discovery] disabled: {exc}")
            return

        try:
            sock.sendto(discovery_hello(self.config), (self.config.discovery_addr, self.config.discovery_port))
            sock.sendto(legacy_discovery_hello(self.config), (self.config.discovery_addr, self.config.discovery_port))
            print(f"[discovery] hello sent to {self.config.discovery_addr}:{self.config.discovery_port}")
        except OSError as exc:
            print(f"[discovery] hello failed: {exc}")

        while not self._stop_event.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except OSError:
                break
            payload = data.decode("utf-8", errors="replace")
            if "Probe" not in payload and "Resolve" not in payload:
                continue
            reply_host = local_ip_for(addr[0])
            if not is_usable_lan_ip(reply_host):
                reply_host = None
            if "Resolve" in payload:
                responses = [
                    discovery_resolve_match(self.config, extract_message_id(payload), reply_host),
                    legacy_discovery_resolve_match(self.config, extract_message_id(payload), reply_host),
                ]
            else:
                responses = [
                    discovery_probe_match(self.config, extract_message_id(payload), reply_host),
                    legacy_discovery_probe_match(self.config, extract_message_id(payload), reply_host),
                ]
            try:
                for response in responses:
                    sock.sendto(response, addr)
                print(f"[discovery] replied to {addr[0]}:{addr[1]} ({len(responses)} variants)")
            except OSError as exc:
                print(f"[discovery] reply failed: {exc}")


def tool_path(name: str) -> Path | None:
    names = [name]
    if os.name == "nt" and not name.lower().endswith(".exe"):
        names.insert(0, f"{name}.exe")
    for root in (install_dir(), app_root()):
        for candidate_name in names:
            candidate = root / candidate_name
            if candidate.exists():
                return candidate
    found = shutil.which(names[0])
    return Path(found) if found else None


class ScreenRtspStream:
    def __init__(self, config: CameraConfig) -> None:
        self.config = config
        self.mediamtx: subprocess.Popen[str] | None = None
        self.ffmpeg: subprocess.Popen[str] | None = None
        self.mediamtx_config = install_dir() / "mediamtx_virtual_onvif.yml"

    def _popen(self, command: list[str], label: str) -> subprocess.Popen[str]:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=str(install_dir()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            creationflags=creationflags,
        )
        threading.Thread(target=self._log_output, args=(process, label), daemon=True).start()
        return process

    def _log_output(self, process: subprocess.Popen[str], label: str) -> None:
        if not process.stdout:
            return
        for line in process.stdout:
            line = line.strip()
            if line:
                print(f"[{label}] {line}")

    def write_mediamtx_config(self) -> Path:
        # Disable UDP RTP ports so they cannot collide with this app's HTTP port 8000.
        text = f"""# Auto-generated by Virtual ONVIF Camera.
logLevel: info
rtsp: true
rtspAddress: :{self.config.rtsp_port}
rtspTransports: [tcp]
rtmp: false
hls: false
webrtc: false
srt: false

paths:
  {self.config.stream_path}:
    source: publisher
  all_others:
    source: publisher
"""
        self.mediamtx_config.write_text(text, encoding="utf-8")
        return self.mediamtx_config

    def capture_backends(self) -> list[str]:
        backend = self.config.capture_backend
        if backend == "auto":
            return ["ddagrab", "gdigrab"]
        if backend in {"ddagrab", "gdigrab"}:
            return [backend]
        print(f"[warn] unknown capture_backend={backend!r}; using auto")
        return ["ddagrab", "gdigrab"]

    def encoders(self) -> list[str]:
        encoder = self.config.encoder or "auto"
        if encoder == "auto":
            # Intel QSV is the most common hardware encoder on office PCs.
            # If the machine only has the Microsoft Basic Display Adapter,
            # this attempt exits quickly and the stream falls back to libx264.
            return ["h264_qsv", "libx264"]
        if encoder in {"h264_nvenc", "h264_qsv", "h264_amf"}:
            return [encoder, "libx264"]
        if encoder == "libx264":
            return ["libx264"]
        print(f"[warn] unknown encoder={encoder!r}; using auto")
        return ["h264_qsv", "libx264"]

    def encoder_args(self, encoder: str) -> list[str]:
        gop = str(keyframe_interval(self.config))
        profile = h264_profile_value(self.config)
        bufsize = bitrate_scaled(self.config.screen_bitrate, 2)
        common_rate = [
            "-b:v",
            self.config.screen_bitrate,
            "-maxrate",
            self.config.screen_bitrate,
            "-bufsize",
            bufsize,
        ]
        if encoder == "h264_nvenc":
            return [
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p4",
                "-profile:v",
                profile,
                "-g",
                gop,
                "-bf",
                "0",
                "-forced-idr",
                "1",
                *common_rate,
            ]
        if encoder == "h264_qsv":
            return [
                "-c:v",
                "h264_qsv",
                "-preset",
                "veryfast",
                "-profile:v",
                profile,
                "-g",
                gop,
                "-bf",
                "0",
                *common_rate,
            ]
        if encoder == "h264_amf":
            return [
                "-c:v",
                "h264_amf",
                "-usage",
                "lowlatency",
                "-quality",
                "speed",
                "-profile:v",
                profile,
                "-g",
                gop,
                "-bf",
            "0",
            *common_rate,
        ]
        return [
            "-c:v",
            "libx264",
            "-preset",
            self.config.encoder_preset,
            "-tune",
            "zerolatency",
            "-profile:v",
            profile,
            "-g",
            gop,
            "-keyint_min",
            gop,
            "-sc_threshold",
            "0",
            "-bf",
            "0",
            "-x264-params",
            f"keyint={gop}:min-keyint={gop}:scenecut=0:repeat-headers=1:aud=1",
            *common_rate,
        ]

    def ffmpeg_command(self, ffmpeg: Path, backend: str, encoder: str) -> list[str]:
        width, height = video_dimensions(self.config)
        scale_filter = f"scale={width}:{height}:flags=fast_bilinear,format=yuv420p"
        base = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "info",
        ]
        if backend == "ddagrab":
            source = (
                f"ddagrab=output_idx={self.config.capture_output}:"
                f"framerate={self.config.screen_fps}:"
                f"draw_mouse={1 if self.config.draw_mouse else 0},"
                f"hwdownload,format=bgra,{scale_filter}"
            )
            input_args = ["-f", "lavfi", "-i", source]
        else:
            input_args = [
                "-thread_queue_size",
                "512",
                "-rtbufsize",
                "256M",
                "-use_wallclock_as_timestamps",
                "1",
                "-f",
                "gdigrab",
                "-draw_mouse",
                "1" if self.config.draw_mouse else "0",
                "-framerate",
                str(self.config.screen_fps),
                "-i",
                "desktop",
                "-vf",
                scale_filter,
            ]
        return [
            *base,
            *input_args,
            *self.encoder_args(encoder),
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-fps_mode",
            "cfr",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            self.config.local_publish_uri,
        ]

    def start(self) -> bool:
        if os.name != "nt":
            print("[screen] disabled: desktop RTSP capture is only enabled on Windows builds")
            return False
        mediamtx = tool_path("mediamtx")
        ffmpeg = tool_path("ffmpeg")
        if not mediamtx or not ffmpeg:
            print("[error] screen stream disabled: mediamtx.exe or ffmpeg.exe was not found next to the app")
            return False

        mediamtx_config = self.write_mediamtx_config()
        print(f"[screen] mediamtx config: {mediamtx_config}")
        self.mediamtx = self._popen([str(mediamtx), str(mediamtx_config)], "mediamtx")
        time.sleep(1.0)
        if self.mediamtx.poll() is not None:
            print(f"[error] screen stream disabled: mediamtx exited early; TCP {self.config.rtsp_port} may already be in use")
            return False
        if not tcp_port_open("127.0.0.1", self.config.rtsp_port):
            print(f"[error] screen stream disabled: RTSP server is not listening on 127.0.0.1:{self.config.rtsp_port}")
            self.stop()
            return False

        print(
            f"[screen] ffmpeg capture: fps={self.config.screen_fps}, "
            f"size={video_dimensions(self.config)[0]}x{video_dimensions(self.config)[1]}, "
            f"bitrate={self.config.screen_bitrate}, profile={h264_profile_value(self.config)}, "
            f"encoder={self.config.encoder}, backend={self.config.capture_backend}"
        )
        for backend in self.capture_backends():
            for encoder in self.encoders():
                print(f"[screen] trying capture backend={backend}, encoder={encoder}")
                self.ffmpeg = self._popen(self.ffmpeg_command(ffmpeg, backend, encoder), "ffmpeg")
                time.sleep(2.0)
                if self.ffmpeg.poll() is None:
                    print(f"[screen] capture backend active: {backend}, encoder active: {encoder}")
                    break
                print(f"[warn] ffmpeg exited early with backend={backend}, encoder={encoder}")
                self.ffmpeg = None
            if self.ffmpeg and self.ffmpeg.poll() is None:
                break
        if not self.ffmpeg or self.ffmpeg.poll() is not None:
            print("[error] screen stream disabled: ffmpeg desktop capture exited early")
            self.stop()
            return False
        if not tcp_port_open("127.0.0.1", self.config.rtsp_port):
            print(f"[error] screen stream disabled: RTSP server is not reachable on 127.0.0.1:{self.config.rtsp_port}")
            self.stop()
            return False
        print(f"[screen] desktop RTSP stream ready: {self.config.stream_uri}")
        print(f"[screen] if the NVR still shows disconnected, allow TCP {self.config.rtsp_port} in Windows Firewall")
        return True

    def stop(self) -> None:
        for process in (self.ffmpeg, self.mediamtx):
            if process and process.poll() is None:
                process.terminate()
        deadline = time.time() + 3
        for process in (self.ffmpeg, self.mediamtx):
            if not process:
                continue
            while process.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if process.poll() is None:
                process.kill()


def parse_args(argv: list[str]) -> argparse.Namespace:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config", default=str(default_config_path()))
    known, _ = bootstrap.parse_known_args(argv)
    config_path = Path(known.config).expanduser().resolve()
    defaults = read_config_defaults(config_path)

    parser = argparse.ArgumentParser(
        description="Virtual ONVIF camera gateway. Exposes ONVIF discovery/SOAP and points clients to an RTSP stream.",
        parents=[bootstrap],
    )
    parser.add_argument("--host", default=defaults["host"], help="HTTP bind host")
    parser.add_argument("--port", type=int, default=int(defaults["port"]), help="HTTP bind port")
    parser.add_argument("--public-host", default=defaults["public_host"], help="IP/host advertised to ONVIF clients")
    parser.add_argument("--rtsp-url", default=defaults["rtsp_url"], help="RTSP stream URI returned by GetStreamUri")
    parser.add_argument("--rtsp-port", type=int, default=int(defaults["rtsp_port"]), help="Built-in desktop RTSP port")
    parser.add_argument(
        "--screen-stream",
        dest="screen_stream",
        action="store_true",
        default=config_bool(defaults["screen_stream"], True),
        help="Start the built-in Windows desktop RTSP stream when rtsp_url is auto",
    )
    parser.add_argument(
        "--no-screen-stream",
        dest="screen_stream",
        action="store_false",
        help="Disable the built-in Windows desktop RTSP stream",
    )
    parser.add_argument("--screen-fps", type=int, default=int(defaults["screen_fps"]), help="Desktop RTSP capture framerate")
    parser.add_argument("--screen-width", type=int, default=int(defaults["screen_width"]), help="Desktop RTSP output width")
    parser.add_argument("--screen-height", type=int, default=int(defaults["screen_height"]), help="Desktop RTSP output height")
    parser.add_argument("--screen-bitrate", default=defaults["screen_bitrate"], help="Desktop RTSP video bitrate, e.g. 6000k")
    parser.add_argument("--capture-backend", default=defaults["capture_backend"], help="Desktop capture backend: auto, ddagrab, or gdigrab")
    parser.add_argument("--capture-output", type=int, default=int(defaults["capture_output"]), help="Desktop output index for ddagrab")
    parser.add_argument(
        "--draw-mouse",
        dest="draw_mouse",
        action="store_true",
        default=config_bool(defaults["draw_mouse"], True),
        help="Draw the mouse cursor in the desktop RTSP stream",
    )
    parser.add_argument(
        "--no-draw-mouse",
        dest="draw_mouse",
        action="store_false",
        help="Do not draw the mouse cursor in the desktop RTSP stream",
    )
    parser.add_argument("--encoder", default=defaults["encoder"], help="H.264 encoder: auto, libx264, h264_nvenc, h264_qsv, or h264_amf")
    parser.add_argument("--encoder-preset", default=defaults["encoder_preset"], help="x264 preset, e.g. veryfast or superfast")
    parser.add_argument("--h264-profile", default=defaults["h264_profile"], help="H.264 profile: baseline, main, or high")
    parser.add_argument("--keyframe-seconds", type=int, default=int(defaults["keyframe_seconds"]), help="Keyframe interval in seconds")
    parser.add_argument("--snapshot-url", default=defaults["snapshot_url"], help="Snapshot URI returned by GetSnapshotUri")
    parser.add_argument("--name", default=defaults["name"], help="Camera/profile name")
    parser.add_argument("--username", default=defaults["username"], help="ONVIF username advertised by GetUsers")
    parser.add_argument("--password", default=defaults["password"], help="ONVIF password accepted by clients")
    parser.add_argument("--manufacturer", default=defaults["manufacturer"], help="ONVIF manufacturer")
    parser.add_argument("--model", default=defaults["model"], help="ONVIF model")
    parser.add_argument("--serial", default=defaults["serial"], help="ONVIF serial number")
    parser.add_argument("--hardware-id", default=defaults["hardware_id"], help="ONVIF hardware id")
    parser.add_argument("--location", default=defaults["location"], help="ONVIF scope location")
    parser.add_argument("--uuid", default=defaults["uuid"], help="Stable camera UUID")
    parser.add_argument("--frame-dir", default=defaults["frame_dir"], help="Directory containing JPEG frames for snapshot/MJPEG")
    parser.add_argument("--frame-seconds", type=int, default=int(defaults["frame_seconds"]), help="Seconds per snapshot frame")
    parser.add_argument("--mjpeg-delay", type=float, default=float(defaults["mjpeg_delay"]), help="Delay between MJPEG frames")
    parser.add_argument(
        "--no-discovery",
        dest="discovery_enabled",
        action="store_false",
        default=config_bool(defaults["discovery"], True),
        help="Disable WS-Discovery",
    )
    parser.add_argument("--discovery-addr", default=defaults["discovery_addr"], help="WS-Discovery multicast address")
    parser.add_argument("--discovery-port", type=int, default=int(defaults["discovery_port"]), help="WS-Discovery UDP port")
    args = parser.parse_args(argv)
    args.config_path = config_path
    return args


def run_camera(config: CameraConfig, stop_event: threading.Event | None = None) -> None:
    if not config.frame_dir.exists():
        print(f"[warn] frame directory does not exist: {config.frame_dir}")
    print(f"[config] uuid: urn:uuid:{config.uuid}")
    print(f"[config] device service: {config.device_service_url}")
    print(f"[config] media service: {config.media_service_url}")
    print(f"[config] rtsp uri: {config.stream_uri}")
    print(f"[config] snapshot uri: {config.effective_snapshot_url}")
    print(f"[config] onvif username: {config.username}")
    if config.public_host.startswith("127."):
        print("[warn] public_host is loopback. NVRs on other machines cannot discover/connect; set public_host to this PC's LAN IP.")

    screen_stream = None
    if config.should_start_screen_stream:
        screen_stream = ScreenRtspStream(config)
        screen_stream.start()
    else:
        print("[screen] disabled: using configured rtsp_url")

    discovery = None
    if config.discovery_enabled:
        discovery = DiscoveryServer(config)
        discovery.start()

    server = ConfiguredHTTPServer((config.host, config.port), VirtualCameraHandler, config)
    print(f"[http] listening on http://{config.host}:{config.port}")
    try:
        if stop_event is None:
            server.serve_forever()
        else:
            server.timeout = 0.5
            while not stop_event.is_set():
                server.handle_request()
    except KeyboardInterrupt:
        print("\n[shutdown] stopping")
    finally:
        if stop_event is None:
            server.shutdown()
        server.server_close()
        if discovery:
            discovery.stop()
        if screen_stream:
            screen_stream.stop()


def service_binary_path() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --service-run'
    return f'"{sys.executable}" "{Path(__file__).resolve()}" --service-run'


def require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Windows service commands can only run on Windows.")


def install_windows_service() -> None:
    require_windows()
    import pywintypes
    import win32service

    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CREATE_SERVICE)
    try:
        try:
            service = win32service.CreateService(
                scm,
                SERVICE_NAME,
                SERVICE_DISPLAY_NAME,
                win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL,
                service_binary_path(),
                None,
                0,
                None,
                None,
                None,
            )
        except pywintypes.error as exc:
            if getattr(exc, "winerror", None) == 1073:
                print(f"[service] {SERVICE_NAME} is already installed")
                return
            raise
        try:
            win32service.ChangeServiceConfig2(
                service,
                win32service.SERVICE_CONFIG_DESCRIPTION,
                SERVICE_DESCRIPTION,
            )
        finally:
            win32service.CloseServiceHandle(service)
    finally:
        win32service.CloseServiceHandle(scm)
    print(f"[service] installed: {SERVICE_DISPLAY_NAME}")


def start_windows_service() -> None:
    require_windows()
    import pywintypes
    import win32serviceutil

    try:
        win32serviceutil.StartService(SERVICE_NAME)
        print(f"[service] started: {SERVICE_DISPLAY_NAME}")
    except pywintypes.error as exc:
        if getattr(exc, "winerror", None) == 1056:
            print(f"[service] already running: {SERVICE_DISPLAY_NAME}")
            return
        raise


def stop_windows_service() -> None:
    require_windows()
    import pywintypes
    import win32serviceutil

    try:
        win32serviceutil.StopService(SERVICE_NAME)
        print(f"[service] stopped: {SERVICE_DISPLAY_NAME}")
    except pywintypes.error as exc:
        if getattr(exc, "winerror", None) in {1060, 1062}:
            print(f"[service] not running: {SERVICE_DISPLAY_NAME}")
            return
        raise


def uninstall_windows_service() -> None:
    require_windows()
    import pywintypes
    import win32serviceutil

    stop_windows_service()
    try:
        win32serviceutil.RemoveService(SERVICE_NAME)
        print(f"[service] uninstalled: {SERVICE_DISPLAY_NAME}")
    except pywintypes.error as exc:
        if getattr(exc, "winerror", None) == 1060:
            print(f"[service] not installed: {SERVICE_DISPLAY_NAME}")
            return
        raise


def run_windows_service_dispatcher() -> int:
    require_windows()
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class VirtualOnvifCameraService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = threading.Event()
            self.stop_handle = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.stop_event.set()
            win32event.SetEvent(self.stop_handle)

        def SvcDoRun(self):
            servicemanager.LogInfoMsg(f"{SERVICE_DISPLAY_NAME} is starting")
            config = CameraConfig(parse_args([]))
            run_camera(config, self.stop_event)
            servicemanager.LogInfoMsg(f"{SERVICE_DISPLAY_NAME} stopped")

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(VirtualOnvifCameraService)
    servicemanager.StartServiceCtrlDispatcher()
    return 0


def handle_service_command(command: str) -> int:
    commands = {
        "--install-service": install_windows_service,
        "--start-service": start_windows_service,
        "--stop-service": stop_windows_service,
        "--uninstall-service": uninstall_windows_service,
    }
    commands[command]()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--service-run":
        return run_windows_service_dispatcher()
    if argv and argv[0] in {"--install-service", "--start-service", "--stop-service", "--uninstall-service"}:
        return handle_service_command(argv[0])

    args = parse_args(argv)
    config = CameraConfig(args)
    print(f"[config] file: {args.config_path}")
    run_camera(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
