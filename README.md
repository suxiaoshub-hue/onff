# onff - Virtual ONVIF Camera

把一台服务器伪装成 ONVIF 网络摄像机，方便接入 NVR、VMS、摄像机平台或 ONVIF Device Manager。

这个项目来自 `onvif-simulator` 的素材和思路，但新增了一个更适合真实接入测试的轻量服务：

- WS-Discovery，支持局域网 ONVIF 搜索发现
- ONVIF Device Service
- ONVIF Media Service
- `GetProfiles`
- `GetStreamUri`
- `GetSnapshotUri`
- 本地 JPEG 快照
- 本地 MJPEG 预览
- GitHub Actions 自动编译 Windows `.exe`

> 说明：这个程序负责把服务器暴露成 ONVIF 摄像机，并把视频流地址返回给客户端。真正的视频流建议使用已有 RTSP 源，或搭配 go2rtc / MediaMTX / FFmpeg 创建 RTSP 流。

## 快速运行

Windows 下载 `VirtualOnvifCamera-windows-x64` 后解压，直接双击：

```text
VirtualOnvifCamera.exe
```

第一次运行会自动生成：

```text
virtual_onvif_camera.ini
```

默认会自动识别本机 IP，监听 `8000` 端口，并把 RTSP 地址设置为：

```text
rtsp://<本机IP>:8554/VirtualCamera
```

如果你没有 RTSP 源，ONVIF 设备仍然可以被发现，也可以打开快照/MJPEG 预览；但 NVR 真正播放主码流时通常需要一个可用 RTSP 源。需要改 RTSP 地址时，编辑 `virtual_onvif_camera.ini` 里的 `rtsp_url` 即可。

启动后浏览器打开：

```text
http://localhost:8000/
```

局域网内其它设备可打开：

```text
http://本机IP:8000/
```

## 安装为 Windows 服务

下载 artifact 解压后，右键管理员运行：

```text
InstallService.bat
```

它会自动安装并启动 `Virtual ONVIF Camera` 服务，不需要手动输入参数。

其它脚本：

```text
StartService.bat
StopService.bat
UninstallService.bat
```

安装服务后，配置仍然读取 exe 同目录的：

```text
virtual_onvif_camera.ini
```

## 接入 NVR / VMS

1. 确保服务器和 NVR 在同一个局域网。
2. 防火墙放行 TCP `8000` 和 UDP `3702`。
3. 如果你使用 RTSP 源，也要放行 RTSP 对应端口，常见是 TCP `554` 或 `8554`。
4. 在 NVR 里搜索 ONVIF 设备，或手动添加：

```text
ONVIF Host: 服务器 IP
ONVIF Port: 8000
ONVIF Path: /onvif/device_service
Username: admin
Password: admin
```

有些 NVR 不支持无认证 ONVIF 设备，或者要求更完整的 Profile S 行为。遇到这种情况，建议先用 ONVIF Device Manager 验证发现和 `GetStreamUri` 是否正常，再针对目标平台补认证或特定接口。

### 录像机搜不到设备

如果软件已经运行，但 NVR 搜不到 `VirtualCamera`，优先检查这些点：

1. 在运行软件的电脑上确认局域网 IP，例如 `192.168.x.x` 或 `10.x.x.x`，不要用 `127.0.0.1`。
2. 在另一台局域网设备上打开 `http://电脑IP:8000/`，能打开才说明 NVR 可以访问 ONVIF HTTP 服务。
3. Windows 防火墙允许 `VirtualOnvifCamera.exe` 入站，或手动放行 TCP `8000` 和 UDP `3702`。
4. 关闭 VPN、代理网卡、虚拟机网卡后重启软件再搜一次，避免自动识别到错误网卡。
5. 如果自动识别 IP 不对，编辑 `virtual_onvif_camera.ini`：

```ini
public_host = 电脑的局域网IP
```

6. 搜索仍失败时，在 NVR 里手动添加：

```text
ONVIF Host: 电脑的局域网IP
ONVIF Port: 8000
ONVIF Path: /onvif/device_service
Username: admin
Password: admin
```

能搜到但连接不上时，重点看控制台是否还有 `[soap] ... -> Unsupported`。如果没有 Unsupported，下一步通常是 RTSP 视频流问题：默认 `rtsp://电脑IP:8554/VirtualCamera` 只是返回给 NVR 的视频地址，本程序本身不创建 8554 RTSP 视频流。需要搭配 go2rtc / MediaMTX / FFmpeg，或把 `rtsp_url` 改成一个已经能播放的真实 RTSP 地址。

## 使用 go2rtc 创建 RTSP 源

示例 `go2rtc.yaml`：

```yaml
streams:
  cam01: ffmpeg:/path/to/video.mp4#video=h264#audio=none
```

启动 go2rtc 后，本程序这样运行：

```bash
python virtual_onvif_camera.py \
  --public-host 192.168.1.50 \
  --rtsp-url rtsp://192.168.1.50:8554/cam01
```

## Windows EXE 编译和下载

推送到 GitHub 后，Actions 会自动编译：

```text
.github/workflows/build-windows-exe.yml
```

编译完成后，到 GitHub 仓库：

```text
Actions -> Build Windows EXE -> Artifacts -> VirtualOnvifCamera-windows-x64
```

下载里面的：

```text
VirtualOnvifCamera.exe
InstallService.bat
UninstallService.bat
StartService.bat
StopService.bat
virtual_onvif_camera.ini
```

直接运行：

```powershell
.\VirtualOnvifCamera.exe
```

## 配置文件

默认配置文件：

```ini
[camera]
host = 0.0.0.0
port = 8000
public_host = auto
rtsp_url = auto
snapshot_url =
name = VirtualCamera
username = admin
password = admin
discovery = true
```

常用字段：

```text
public_host       对外广播给 NVR 的服务器 IP，auto 为自动识别
rtsp_url          GetStreamUri 返回的 RTSP 地址，auto 为 rtsp://<本机IP>:8554/VirtualCamera
port              HTTP / ONVIF 服务端口，默认 8000
name              摄像机名称，默认 VirtualCamera
username/password ONVIF 连接账号，默认 admin / admin
discovery         是否开启 WS-Discovery
```

## 高级参数

```text
--host              HTTP 监听地址，默认 0.0.0.0
--port              HTTP / ONVIF 服务端口，默认 8000
--public-host       对外广播给 NVR 的服务器 IP
--rtsp-url          GetStreamUri 返回的 RTSP 地址
--snapshot-url      GetSnapshotUri 返回的快照地址，不填则使用本程序 /snapshot.jpg
--name              摄像机名称，默认 VirtualCamera
--manufacturer      厂商名
--model             型号
--serial            序列号
--uuid              固定设备 UUID
--frame-dir         本地 JPEG 帧目录
--no-discovery      关闭 WS-Discovery
```

也可以通过环境变量设置：

```text
ONVIF_PUBLIC_HOST
ONVIF_RTSP_URL
ONVIF_SNAPSHOT_URL
ONVIF_NAME
ONVIF_HTTP_PORT
ONVIF_UUID
```

## 本地测试

```bash
python -m unittest discover -s tests
```

## 推送到你的 GitHub

如果你已经在 GitHub 创建了空仓库，比如：

```text
https://github.com/YOUR_NAME/virtual-onvif-camera.git
```

在本地执行：

```bash
git remote add github https://github.com/YOUR_NAME/virtual-onvif-camera.git
git push -u github main
```

如果你用 SSH：

```bash
git remote add github git@github.com:YOUR_NAME/virtual-onvif-camera.git
git push -u github main
```
