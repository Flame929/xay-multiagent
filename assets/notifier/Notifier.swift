// 「AI 团队看板」原生通知小应用（Bundle ID: local.xay.board-notifier）
// 由 scripts/xay-install-notifier 用 swiftc 编译，装到 ~/Applications/AI 团队看板.app。
//
// 两种启动方式：
//   1. 发通知：读取 ~/.xay/notify/*.msg（四行：标题、副标题、正文、看板地址），
//      或命令行参数 --title/--subtitle/--body/--url（也可按位置给：标题 副标题 正文 地址），
//      逐条用 UNUserNotificationCenter 发出，地址放进通知的 userInfo["url"]。发完数秒后自动退出。
//   2. 用户点击通知：系统唤起本应用并回调 didReceive，打开「这条通知自己的」地址；
//      若看板服务没在运行，先在后台启动 xay_watch.py。处理完自动退出。
// 不常驻：任何情况下最多运行 maxLifetime 秒。

import AppKit
import Foundation
import UserNotifications

let defaultURL = "http://127.0.0.1:7878/"
let queueDir = (NSHomeDirectory() as NSString).appendingPathComponent(".xay/notify")
let watchScript = (NSHomeDirectory() as NSString)
    .appendingPathComponent(".agents/skills/xay-multiagent-workflow/scripts/xay_watch.py")
let lingerAfterPost: TimeInterval = 6      // 发完通知后再留几秒，顺便接住刚写进来的新通知
let waitForClick: TimeInterval = 3         // 没有待发通知时，等系统送来“点击”回调的时间
let maxLifetime: TimeInterval = 120        // 兜底：最长运行时间（含首次授权弹窗等待）

struct Message {
    var title: String
    var subtitle: String
    var body: String
    var url: String
}

func log(_ s: String) {
    FileHandle.standardError.write(("[xay-notifier] " + s + "\n").data(using: .utf8)!)
}

// 解析命令行参数；没有可发内容时返回 nil
func messageFromArguments() -> Message? {
    var args = Array(CommandLine.arguments.dropFirst())
    // LaunchServices 可能塞进 -psn_xxx / -NSDocumentRevisionsDebugMode 之类的参数，去掉
    args.removeAll { $0.hasPrefix("-psn_") }
    if let i = args.firstIndex(of: "-NSDocumentRevisionsDebugMode"), i + 1 < args.count {
        args.removeSubrange(i...(i + 1))
    }
    if args.isEmpty { return nil }
    var m = Message(title: "AI 团队看板", subtitle: "", body: "", url: defaultURL)
    var positional: [String] = []
    var i = 0
    var sawFlag = false
    while i < args.count {
        let a = args[i]
        let next: String? = i + 1 < args.count ? args[i + 1] : nil
        switch a {
        case "--title": if let v = next { m.title = v; i += 1; sawFlag = true }
        case "--subtitle": if let v = next { m.subtitle = v; i += 1; sawFlag = true }
        case "--body": if let v = next { m.body = v; i += 1; sawFlag = true }
        case "--url": if let v = next { m.url = v; i += 1; sawFlag = true }
        default: positional.append(a)
        }
        i += 1
    }
    if !sawFlag {
        if positional.isEmpty { return nil }
        let p = positional + ["", "", "", ""]
        m.title = p[0].isEmpty ? m.title : p[0]
        m.subtitle = p[1]; m.body = p[2]
        if !p[3].isEmpty { m.url = p[3] }
    }
    if m.body.isEmpty && m.subtitle.isEmpty { return nil }
    return m
}

// 取走队列里所有 .msg（读完即删，避免重复发）
func drainQueue() -> [Message] {
    let fm = FileManager.default
    try? fm.createDirectory(atPath: queueDir, withIntermediateDirectories: true)
    guard let names = try? fm.contentsOfDirectory(atPath: queueDir) else { return [] }
    var out: [Message] = []
    for n in names.filter({ $0.hasSuffix(".msg") }).sorted() {
        let p = (queueDir as NSString).appendingPathComponent(n)
        defer { try? fm.removeItem(atPath: p) }
        guard let text = try? String(contentsOfFile: p, encoding: .utf8) else { continue }
        var lines = text.components(separatedBy: "\n")
        while lines.count < 4 { lines.append("") }
        let url = lines[3].trimmingCharacters(in: .whitespacesAndNewlines)
        out.append(Message(title: lines[0].isEmpty ? "AI 团队看板" : lines[0],
                           subtitle: lines[1], body: lines[2],
                           url: url.isEmpty ? defaultURL : url))
    }
    return out
}

// 看板端口：本机地址取 URL 里的端口，否则 7878
func boardPort(for url: URL?) -> Int {
    if let u = url, let h = u.host, h == "127.0.0.1" || h == "localhost", let p = u.port { return p }
    return 7878
}

func portIsOpen(_ port: Int) -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    if fd < 0 { return false }
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = in_port_t(UInt16(port).bigEndian)
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let r = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return r == 0
}

// 确保看板服务在运行；没在跑就后台启动 xay_watch.py，最多等 6 秒
func ensureBoardRunning(port: Int) {
    if portIsOpen(port) { return }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/sh")
    let portArg = port == 7878 ? "" : " --port \(port)"
    p.arguments = ["-c",
        "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH; " +
        "(nohup python3 \"$0\"\(portArg) >/dev/null 2>&1 &)", watchScript]
    do { try p.run(); p.waitUntilExit() } catch { log("启动看板失败：\(error)"); return }
    let deadline = Date().addingTimeInterval(6)
    while Date() < deadline {
        if portIsOpen(port) { return }
        Thread.sleep(forTimeInterval: 0.2)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let center = UNUserNotificationCenter.current()
    var exitTimer: Timer?
    var handledClick = false
    var authorized: Bool? = nil
    var pending: [Message] = []
    var pollTimer: Timer?

    // 必须在启动完成前设好 delegate，才能接住“点击通知唤起本应用”的回调
    func applicationWillFinishLaunching(_ notification: Notification) {
        center.delegate = self
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        Timer.scheduledTimer(withTimeInterval: maxLifetime, repeats: false) { _ in NSApp.terminate(nil) }
        if let m = messageFromArguments() { pending.append(m) }
        pending += drainQueue()
        if pending.isEmpty {
            // 多半是用户点了通知：等 didReceive；等不到就当作手动打开，打开看板首页
            scheduleExit(after: waitForClick) { [weak self] in
                guard let self = self, !self.handledClick else { return }
                self.handledClick = true
                self.openBoard(defaultURL)
            }
            return
        }
        startSending()
    }

    var authRequested = false
    func startSending() {
        if authRequested { flush(); return }
        authRequested = true
        center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            DispatchQueue.main.async {
                self.authorized = granted
                if let e = error { log("授权出错：\(e.localizedDescription)") }
                if !granted { log("未获通知授权，请在“系统设置 → 通知 → AI 团队看板”中允许") }
                self.flush()
                self.startPolling()
            }
        }
    }

    // 已在运行时又被 open 唤起（xay-notify 写了新 .msg）
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        pending += drainQueue()
        if !pending.isEmpty { startSending() }
        return false
    }

    func startPolling() {
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            let more = drainQueue()
            if !more.isEmpty { self.pending += more; self.flush() }
        }
    }

    func flush() {
        guard authorized != nil else { return }   // 还在等授权结果
        let batch = pending
        pending.removeAll()
        for m in batch { post(m) }
        if !handledClick { scheduleExit(after: lingerAfterPost) }
    }

    func post(_ m: Message) {
        let c = UNMutableNotificationContent()
        c.title = m.title
        c.subtitle = m.subtitle
        c.body = m.body
        c.userInfo = ["url": m.url]
        c.threadIdentifier = m.url
        let req = UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil)
        center.add(req) { error in
            if let e = error { log("发通知失败：\(e.localizedDescription)") } else { log("已发：\(m.subtitle) | \(m.body) | \(m.url)") }
        }
    }

    func scheduleExit(after t: TimeInterval, before: (() -> Void)? = nil) {
        exitTimer?.invalidate()
        exitTimer = Timer.scheduledTimer(withTimeInterval: t, repeats: false) { _ in
            before?()
            NSApp.terminate(nil)
        }
    }

    func openBoard(_ urlString: String) {
        let url = URL(string: urlString) ?? URL(string: defaultURL)!
        ensureBoardRunning(port: boardPort(for: url))
        NSWorkspace.shared.open(url)
        log("已打开：\(url.absoluteString)")
    }

    // 应用在前台时也照常弹横幅
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    // 点击通知：打开这条通知自己的地址
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        handledClick = true
        let url = response.notification.request.content.userInfo["url"] as? String ?? defaultURL
        if response.actionIdentifier == UNNotificationDefaultActionIdentifier {
            DispatchQueue.global().async {
                self.openBoard(url)
                DispatchQueue.main.async {
                    completionHandler()
                    // 若同时还有通知要发，给它们留时间；否则马上退出
                    self.scheduleExit(after: self.pending.isEmpty ? 0.5 : lingerAfterPost)
                }
            }
        } else {
            completionHandler()
            scheduleExit(after: 0.5)
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
