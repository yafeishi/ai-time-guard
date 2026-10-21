import AppKit
import AVFoundation
import Vision
import CoreMedia

func emit(_ values: [String: Any]) {
    var data = values
    data["time"] = Date().timeIntervalSince1970
    guard JSONSerialization.isValidJSONObject(data), let bytes = try? JSONSerialization.data(withJSONObject: data),
          let line = String(data: bytes, encoding: .utf8) else { return }
    print(line)
    fflush(stdout)
}

func availableDevices() -> [AVCaptureDevice] {
    AVCaptureDevice.devices(for: .video)
}

// Eye contour coordinates are normalized to the face rectangle. Correct the
// aspect ratio before measuring vertical opening relative to the eye's axis.
func eyeOpening(_ eye: VNFaceLandmarkRegion2D, face: VNFaceObservation, width: Int, height: Int) -> Double? {
    let points = eye.normalizedPoints.map { CGPoint(x: Double($0.x) * face.boundingBox.width * Double(width),
                                                    y: Double($0.y) * face.boundingBox.height * Double(height)) }
    guard points.count >= 6 else { return nil }
    var longest = 0.0
    var axis = CGPoint.zero
    for a in points {
        for b in points {
            let dx = b.x-a.x, dy = b.y-a.y
            let length = hypot(dx, dy)
            if length > longest { longest = length; axis = CGPoint(x: dx, y: dy) }
        }
    }
    guard longest > 6 else { return nil }
    let x = axis.x / longest, y = axis.y / longest
    let normal = points.map { -$0.x*y + $0.y*x }
    guard let low = normal.min(), let high = normal.max() else { return nil }
    return (high-low)/longest
}

final class CameraCapture: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let queue = DispatchQueue(label: "com.dang.ai-time-guard.camera.frames", qos: .userInitiated)
    var lastFrame = 0.0
    var lastHumanCheck = 0.0
    var humanVisible = false
    let selectedDevice: String?
    let trackBlinks: Bool
    init(device: String?, trackBlinks: Bool) { self.selectedDevice = device; self.trackBlinks = trackBlinks }

    func start() {
        do {
            let devices = availableDevices()
            let chosen = selectedDevice.flatMap { id in devices.first { $0.uniqueID == id } } ?? (selectedDevice == nil ? AVCaptureDevice.default(for: .video) : nil)
            guard let device = chosen else { emit(["status": "error", "error": "未找到摄像头，请检查连接或重新选择设备"]); return }
            let input = try AVCaptureDeviceInput(device: device)
            session.beginConfiguration()
            session.sessionPreset = .vga640x480
            guard session.canAddInput(input) else { session.commitConfiguration(); emit(["status": "error", "error": "摄像头暂不可用，可能被其他应用占用"]); return }
            session.addInput(input)
            let output = AVCaptureVideoDataOutput()
            output.alwaysDiscardsLateVideoFrames = true
            output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
            output.setSampleBufferDelegate(self, queue: queue)
            guard session.canAddOutput(output) else { session.commitConfiguration(); emit(["status": "error", "error": "无法读取摄像头画面"]); return }
            session.addOutput(output)
            session.commitConfiguration()
            session.startRunning()
            emit(["status": "running", "device": device.localizedName])
        } catch {
            emit(["status": "error", "error": "无法启动摄像头，请检查系统权限和设备连接"])
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        let now = Date().timeIntervalSince1970
        guard now-lastFrame >= 1.0/15, let pixel = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lastFrame = now
        autoreleasepool {
            do {
                let handler = VNImageRequestHandler(cvPixelBuffer: pixel, orientation: .up, options: [:])
                let faces: [VNFaceObservation]
                if trackBlinks {
                    let request = VNDetectFaceLandmarksRequest()
                    try handler.perform([request])
                    faces = (request.results ?? []).filter { $0.confidence >= 0.6 }
                } else {
                    let request = VNDetectFaceRectanglesRequest()
                    try handler.perform([request])
                    faces = (request.results ?? []).filter { $0.confidence >= 0.6 }
                }
                if faces.isEmpty && now-lastHumanCheck >= 1 {
                    let people = VNDetectHumanRectanglesRequest()
                    people.upperBodyOnly = true
                    try handler.perform([people])
                    humanVisible = (people.results ?? []).contains { $0.confidence >= 0.6 }
                    lastHumanCheck = now
                }
                var data: [String: Any] = ["status": "frame", "faces": faces.count,
                                          "person": !faces.isEmpty || (humanVisible && now-lastHumanCheck <= 1.5)]
                if faces.count == 1, let face = faces.first, let landmarks = face.landmarks,
                   let left = landmarks.leftEye, let right = landmarks.rightEye,
                   let l = eyeOpening(left, face: face, width: CVPixelBufferGetWidth(pixel), height: CVPixelBufferGetHeight(pixel)),
                   let r = eyeOpening(right, face: face, width: CVPixelBufferGetWidth(pixel), height: CVPixelBufferGetHeight(pixel)) {
                    // Both eyes need to close; a single wink is not a full blink.
                    data["opening"] = max(l, r)
                }
                emit(data)
            } catch {
                emit(["status": "error", "error": "人脸检测暂时不可用"])
            }
        }
    }
}

final class BlinkHint {
    var panel: NSPanel?
    var hideTimer: Timer?
    func setVisible(_ visible: Bool) {
        hideTimer?.invalidate()
        guard visible else { panel?.orderOut(nil); return }
        if panel == nil {
            let screen = NSScreen.main ?? NSScreen.screens[0]
            let bounds = screen.visibleFrame
            let rect = NSRect(x: bounds.maxX-320, y: bounds.minY+30, width: 290, height: 76)
            let window = NSPanel(contentRect: rect, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            window.level = .floating
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            window.hidesOnDeactivate = false
            window.isOpaque = false
            window.backgroundColor = NSColor.windowBackgroundColor.withAlphaComponent(0.97)
            window.contentView?.wantsLayer = true
            window.contentView?.layer?.cornerRadius = 12
            let title = NSTextField(labelWithString: "眨眨眼，放松一下")
            title.frame = NSRect(x: 20, y: 38, width: 250, height: 22)
            title.font = .systemFont(ofSize: 16, weight: .medium)
            let caption = NSTextField(labelWithString: "近期频率低于你设置的目标")
            caption.frame = NSRect(x: 20, y: 15, width: 250, height: 18)
            caption.font = .systemFont(ofSize: 12)
            caption.textColor = .secondaryLabelColor
            window.contentView?.addSubview(title)
            window.contentView?.addSubview(caption)
            panel = window
        }
        panel?.orderFrontRegardless()
        hideTimer = Timer.scheduledTimer(withTimeInterval: 8, repeats: false) { [weak self] _ in self?.panel?.orderOut(nil) }
    }
}

let arguments = CommandLine.arguments
if arguments.contains("--list-devices") {
    emit(["status": "devices", "devices": availableDevices().map { ["id": $0.uniqueID, "name": $0.localizedName] }])
    exit(0)
}
if arguments.contains("--check") {
    let permission = AVCaptureDevice.authorizationStatus(for: .video)
    emit(["status": "permission", "permission": permission.rawValue])
    exit(0)
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.finishLaunching()
let index = arguments.firstIndex(of: "--device")
let device = index.flatMap { $0+1 < arguments.count ? arguments[$0+1] : nil }
let capture = CameraCapture(device: device, trackBlinks: !arguments.contains("--presence-only"))
let hint = BlinkHint()
DispatchQueue.global(qos: .utility).async {
    while let line = readLine() {
        if let bytes = line.data(using: .utf8), let values = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any],
           let visible = values["hint"] as? Bool {
            DispatchQueue.main.async { hint.setVisible(visible) }
        }
    }
    // Parent app has exited: release the camera instead of lingering.
    DispatchQueue.main.async { capture.session.stopRunning(); app.terminate(nil) }
}
func startCamera() {
    DispatchQueue.global(qos: .userInitiated).async { capture.start() }
}
switch AVCaptureDevice.authorizationStatus(for: .video) {
case .authorized: startCamera()
case .notDetermined:
    emit(["status": "awaiting_permission"])
    AVCaptureDevice.requestAccess(for: .video) { allowed in
        if allowed { startCamera() }
        else { emit(["status": "denied", "error": "摄像头权限未获允许，请在系统设置中允许 AI Time Guard Camera 访问摄像头"] ) }
    }
default: emit(["status": "denied", "error": "摄像头权限未获允许，请在系统设置中允许 AI Time Guard Camera 访问摄像头"])
}
// Stop cleanly so the camera indicator turns off immediately on disable.
signal(SIGTERM, SIG_IGN)
let termination = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
termination.setEventHandler { capture.session.stopRunning(); app.terminate(nil) }
termination.resume()
app.run()
