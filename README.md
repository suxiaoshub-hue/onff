# Virtual ONVIF Camera

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

Python 3.10+：

```bash
python virtual_onvif_camera.py --rtsp-url rtsp://192.168.1.10:8554/live
```

指定对外 IP、端口和名称：

```bash
python virtual_onvif_camera.py \
  --public-host 192.168.1.50 \
  --port 8000 \
  --name ServerCam01 \
  --rtsp-url rtsp://192.168.1.50:8554/cam01
```

启动后浏览器打开：

```text
http://192.168.1.50:8000/
```

ONVIF 地址：

```text
http://192.168.1.50:8000/onvif/device_service
```

快照地址：

```text
http://192.168.1.50:8000/snapshot.jpg
```

MJPEG 预览：

```text
http://192.168.1.50:8000/mjpeg/1
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
Username: 留空
Password: 留空
```

有些 NVR 不支持无认证 ONVIF 设备，或者要求更完整的 Profile S 行为。遇到这种情况，建议先用 ONVIF Device Manager 验证发现和 `GetStreamUri` 是否正常，再针对目标平台补认证或特定接口。

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

## Windows EXE 编译

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
```

运行示例：

```powershell
.\VirtualOnvifCamera.exe --public-host 192.168.1.50 --rtsp-url rtsp://192.168.1.50:8554/cam01
```

## 配置参数

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
