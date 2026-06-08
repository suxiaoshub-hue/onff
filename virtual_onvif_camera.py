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
import socket
import socketserver
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


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
snapshot_url =
name = VirtualCamera
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
        "snapshot_url": section.get("snapshot_url", os.getenv("ONVIF_SNAPSHOT_URL", "")),
        "name": section.get("name", os.getenv("ONVIF_NAME", "VirtualCamera")),
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
        self.manufacturer = args.manufacturer
        self.model = args.model
        self.serial = args.serial
        self.hardware_id = args.hardware_id
        self.location = args.location
        self.uuid = normalize_uuid(args.uuid or None)
        self.rtsp_url = "" if args.rtsp_url == "auto" else args.rtsp_url
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
    def stream_uri(self) -> str:
        return self.rtsp_url or f"rtsp://{self.public_host}:8554/{self.name}"

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
        <tds:Security TLS1.1="false" TLS1.2="false" OnboardKeyGeneration="false" AccessPolicyConfig="false" X.509Token="false" SAMLToken="false" KerberosToken="false" RELToken="false"/>
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
      <tds:NetworkProtocols><tt:Name>RTSP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>8554</tt:Port></tds:NetworkProtocols>
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


def get_users() -> bytes:
    return soap_envelope(
        """<tds:GetUsersResponse/>""",
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


def profile_xml(config: CameraConfig) -> str:
    return f"""<trt:Profiles fixed="true" token="profile_1">
      <tt:Name>{xml_escape(config.name)}</tt:Name>
      <tt:VideoSourceConfiguration token="video_source_config_1">
        <tt:Name>VideoSourceConfig</tt:Name>
        <tt:UseCount>1</tt:UseCount>
        <tt:SourceToken>video_source_1</tt:SourceToken>
        <tt:Bounds x="0" y="0" width="1920" height="1080"/>
      </tt:VideoSourceConfiguration>
      <tt:VideoEncoderConfiguration token="video_encoder_config_1">
        <tt:Name>H264</tt:Name>
        <tt:UseCount>1</tt:UseCount>
        <tt:Encoding>H264</tt:Encoding>
        <tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
        <tt:Quality>5</tt:Quality>
        <tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>
        <tt:H264><tt:GovLength>50</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>
        <tt:SessionTimeout>PT60S</tt:SessionTimeout>
      </tt:VideoEncoderConfiguration>
    </trt:Profiles>"""


def get_profiles(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"<trt:GetProfilesResponse>{profile_xml(config)}</trt:GetProfilesResponse>",
        f"{TRT}/GetProfilesResponse",
    )


def get_video_sources(config: CameraConfig) -> bytes:
    return soap_envelope(
        f"""<trt:GetVideoSourcesResponse>
      <trt:VideoSources token="video_source_1">
        <tt:Framerate>25</tt:Framerate>
        <tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
        <tt:Imaging><tt:Brightness>50</tt:Brightness><tt:ColorSaturation>50</tt:ColorSaturation><tt:Contrast>50</tt:Contrast><tt:Sharpness>50</tt:Sharpness></tt:Imaging>
      </trt:VideoSources>
    </trt:GetVideoSourcesResponse>""",
        f"{TRT}/GetVideoSourcesResponse",
    )


def video_encoder_configuration_xml() -> str:
    return """<trt:Configurations token="video_encoder_config_1">
      <tt:Name>H264</tt:Name>
      <tt:UseCount>1</tt:UseCount>
      <tt:Encoding>H264</tt:Encoding>
      <tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
      <tt:Quality>5</tt:Quality>
      <tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>
      <tt:H264><tt:GovLength>50</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>
      <tt:SessionTimeout>PT60S</tt:SessionTimeout>
    </trt:Configurations>"""


def get_video_encoder_configurations() -> bytes:
    return soap_envelope(
        f"<trt:GetVideoEncoderConfigurationsResponse>{video_encoder_configuration_xml()}</trt:GetVideoEncoderConfigurationsResponse>",
        f"{TRT}/GetVideoEncoderConfigurationsResponse",
    )


def get_video_encoder_configuration() -> bytes:
    return soap_envelope(
        f"<trt:GetVideoEncoderConfigurationResponse>{video_encoder_configuration_xml()}</trt:GetVideoEncoderConfigurationResponse>",
        f"{TRT}/GetVideoEncoderConfigurationResponse",
    )


def get_video_encoder_configuration_options() -> bytes:
    return soap_envelope(
        """<trt:GetVideoEncoderConfigurationOptionsResponse>
      <trt:Options>
        <tt:QualityRange><tt:Min>1</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>
        <tt:H264>
          <tt:ResolutionsAvailable><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:ResolutionsAvailable>
          <tt:ResolutionsAvailable><tt:Width>1280</tt:Width><tt:Height>720</tt:Height></tt:ResolutionsAvailable>
          <tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>120</tt:Max></tt:GovLengthRange>
          <tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>30</tt:Max></tt:FrameRateRange>
          <tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>
          <tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported>
        </tt:H264>
      </trt:Options>
    </trt:GetVideoEncoderConfigurationOptionsResponse>""",
        f"{TRT}/GetVideoEncoderConfigurationOptionsResponse",
    )


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
        "GetVideoEncoderConfigurationOptions",
        "GetVideoEncoderConfigurations",
        "GetVideoEncoderConfiguration",
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
        "GetProfiles",
        "GetServices",
        "GetHostname",
        "GetScopes",
        "GetUsers",
    ]:
        if action in payload:
            return action
    return "Unsupported"


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
        return get_users()
    if action == "GetScopes":
        return get_scopes(config)
    if action == "GetSystemDateAndTime":
        return get_system_date_and_time()
    if action == "GetProfiles":
        return get_profiles(config)
    if action == "GetVideoSources":
        return get_video_sources(config)
    if action == "GetVideoEncoderConfigurations":
        return get_video_encoder_configurations()
    if action == "GetVideoEncoderConfiguration":
        return get_video_encoder_configuration()
    if action == "GetVideoEncoderConfigurationOptions":
        return get_video_encoder_configuration_options()
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
        print(f"[soap] {self.client_address[0]} -> {action}")
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
    parser.add_argument("--snapshot-url", default=defaults["snapshot_url"], help="Snapshot URI returned by GetSnapshotUri")
    parser.add_argument("--name", default=defaults["name"], help="Camera/profile name")
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
    if config.public_host.startswith("127."):
        print("[warn] public_host is loopback. NVRs on other machines cannot discover/connect; set public_host to this PC's LAN IP.")

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
