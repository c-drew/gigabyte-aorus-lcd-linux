import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.c-drew.aorus-lcd"
  ipcTarget: "io.github.c-drew.aorus-lcd"
  manageIpc: false

  property Item anchorItem: null
  property Item hostWidget: null
  readonly property var barIdentity: hostWidget || root

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string baseUrl: settings && settings.url
    ? String(settings.url).replace(/\/+$/, "") : "http://127.0.0.1:5090"

  // ---- state from /api/status --------------------------------------------------
  property var status: null
  property string lastError: ""
  property string notice: ""
  readonly property bool connected: status !== null && lastError === ""
  readonly property var panelInfo: status ? status.panel : ({})
  readonly property var content: status ? status.settings.content : ({})
  readonly property var overlay: status ? status.settings.overlay : ({})
  readonly property var ss: status && status.screensaver ? status.screensaver : ({})
  readonly property var bg: ss.background || ({})
  readonly property var effects: status && status.ttfx ? (status.ttfx.effects || []) : []
  readonly property bool isScreensaver: content.type === "ttfx"
  readonly property var playlist: isScreensaver && content.effects && content.effects.length
    ? content.effects : (status && status.ttfx ? status.ttfx.default || [] : [])
  readonly property bool jobRunning: status ? status.job.state === "running" : false
  readonly property bool working: jobRunning || bg.phase === "uploading"
  readonly property var intervals: [[0, "Off"], [15, "15m"], [60, "1h"], [180, "3h"], [1440, "1d"]]

  readonly property string showingText: {
    if (!connected) return lastError !== "" ? "Service unavailable" : "Connecting…"
    if (!panelInfo.connected) return "Panel offline"
    if (!panelInfo.on) return "Screen off"
    if (isScreensaver && ss.rotating) return "Screensaver · " + (ss.effect || "…")
    if (isScreensaver) return "Screensaver loop"
    return ({logo: "Logo", image: "Image", gif: "GIF", text: "Text", builtin: "Built-in screen"})[content.type] || "—"
  }
  readonly property string statusText: "AORUS LCD · " + showingText

  readonly property string screensaverLine: {
    if (!isScreensaver) return "Not playing: pick effects, then Play."
    if (jobRunning) return status.job.total
      ? "Writing to the panel " + Math.round(100 * status.job.done / status.job.total) + "%"
        + (status.job.eta_seconds ? ", about " + Math.max(1, Math.round(status.job.eta_seconds / 60)) + " min left" : "")
      : (status.job.label || "Preparing…")
    if (!ss.rotating) return "One loop of the chosen effects."
    var line = "Next: " + (ss.next || "?")
    if (bg.phase === "uploading") {
      line += " · writing " + (bg.total ? Math.round(100 * bg.done / bg.total) + "%" : "(erasing)")
      if (bg.eta_seconds) line += ", about " + Math.max(1, Math.round(bg.eta_seconds / 60)) + " min left"
      line += " (the screen holds still)"
    }
    else if (status.settling_seconds > 0) line += " · the last upload failed; retrying"
    else if (bg.phase === "error") line += " · last swap failed: " + bg.error
    else {
      var mins = Math.max(0, Math.round((ss.due_at - Date.now() / 1000) / 60))
      line += mins > 0 ? " in " + mins + " min" : " shortly"
    }
    if (ss.wear) line += " · flash wear " + ss.wear.used_percent + "%"
    return line
  }

  // ---- requests (lib/aorus-lcd-request: curl to the loopback service) ---------------
  property var queue: []

  function helperPath() {
    return String(Qt.resolvedUrl("lib/aorus-lcd-request")).replace(/^file:\/\//, "")
  }

  function refresh() {
    if (statusProcess.running) return
    statusProcess.command = [helperPath(), "GET", baseUrl + "/api/status", "5"]
    statusProcess.running = true
  }

  function post(path, body, okNotice) {
    queue = queue.concat([{ path: path, body: JSON.stringify(body || {}), notice: okNotice || "" }])
    pump()
  }

  function pump() {
    if (actionProcess.running || queue.length === 0) return
    var next = queue[0]
    queue = queue.slice(1)
    actionProcess.pendingBody = next.body
    actionProcess.pendingNotice = next.notice
    actionProcess.command = [helperPath(), "POST", baseUrl + next.path, "30"]
    actionProcess.running = true
  }

  function setContent(values, okNotice) {
    var next = {}
    for (var key in content) next[key] = content[key]
    for (var k in values) next[k] = values[k]
    post("/api/content", { content: next }, okNotice)
  }

  function toggleEffect(name) {
    var list = playlist.slice()
    var at = list.indexOf(name)
    if (at >= 0) {
      if (list.length === 1) { notice = "Keep at least one effect"; return }
      list.splice(at, 1)
    } else list.push(name)
    if (isScreensaver) setContent({ effects: list }, "Playlist updated")
    else pendingPlaylist = list
  }

  property var pendingPlaylist: null
  readonly property var shownPlaylist: pendingPlaylist || playlist

  function play() {
    setContent({ type: "ttfx", effects: shownPlaylist, rotate_minutes: content.rotate_minutes || 60 },
               "Writing the screensaver to the panel")
    pendingPlaylist = null
  }

  Process {
    id: statusProcess
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) { root.lastError = "Cannot reach " + root.baseUrl; return }
      try { root.status = JSON.parse(statusOut.text || "{}"); root.lastError = "" }
      catch (error) { root.lastError = "Unexpected reply from aorus-lcd" }
    }
  }

  Process {
    id: actionProcess
    property string pendingBody: ""
    property string pendingNotice: ""
    stdinEnabled: true
    stdout: StdioCollector { id: actionOut; waitForEnd: true }
    onStarted: actionProcess.write(actionProcess.pendingBody + "\n")
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        var message = "Request failed"
        try { message = JSON.parse(actionOut.text).error || message } catch (error) {}
        root.notice = message
      } else if (actionProcess.pendingNotice !== "") root.notice = actionProcess.pendingNotice
      Qt.callLater(root.refresh)
      Qt.callLater(root.pump)
    }
  }

  Timer {
    interval: root.opened ? 2000 : 30000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer { id: noticeTimer; interval: 5000; onTriggered: root.notice = "" }
  onNoticeChanged: if (notice !== "") noticeTimer.restart()
  onOpenedChanged: if (opened) { pendingPlaylist = null; refresh() }

  // ---- the popup -------------------------------------------------------------------------
  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(440))
    contentHeight: panel.fittedContentHeight(body.implicitHeight, Style.space(680))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: body
        width: parent.width
        spacing: Style.space(12)

        PanelHero {
          width: parent.width
          title: "AORUS LCD"
          meta: root.showingText
          detail: root.connected && root.panelInfo.firmware ? "LCD firmware " + root.panelInfo.firmware : ""
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            Text {
              text: "\u{F08AE}"
              color: root.connected ? root.foreground : root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }
        }

        Text {
          visible: text !== ""
          width: parent.width
          text: root.notice !== "" ? root.notice : root.lastError
          color: root.lastError !== "" && root.notice === "" ? root.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }

        Row {
          visible: root.connected
          width: parent.width
          spacing: Style.space(8)

          Toggle {
            width: (parent.width - parent.spacing) / 2
            label: "Screen"
            description: root.panelInfo.on ? "On" : "Off"
            checked: root.panelInfo.on === true
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.post("/api/power", { on: !root.panelInfo.on })
          }

          Toggle {
            width: (parent.width - parent.spacing) / 2
            label: "GPU stats"
            description: root.overlay.enabled ? "Over the screen" : "Hidden"
            checked: root.overlay.enabled === true
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.post("/api/overlay", { overlay: { enabled: !root.overlay.enabled } })
          }
        }

        PanelSeparator { visible: root.connected; foreground: root.foreground }

        Column {
          visible: root.connected && root.effects.length > 0
          width: parent.width
          spacing: Style.space(8)

          PanelSectionHeader {
            text: "SCREENSAVER"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            width: parent.width
            text: root.screensaverLine
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Flow {
            width: parent.width
            spacing: Style.space(5)

            Repeater {
              model: root.effects
              Button {
                required property var modelData
                text: String(modelData)
                fontSize: Style.font.caption
                horizontalPadding: Style.space(7)
                verticalPadding: Style.space(3)
                bordered: true
                active: root.shownPlaylist.indexOf(String(modelData)) >= 0
                foreground: root.foreground
                fontFamily: root.fontFamily
                enabled: !root.jobRunning
                onClicked: root.toggleEffect(String(modelData))
              }
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(6)

            Text {
              anchors.verticalCenter: parent.verticalCenter
              text: "Rotate"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Repeater {
              model: root.intervals
              Button {
                required property var modelData
                text: modelData[1]
                fontSize: Style.font.caption
                horizontalPadding: Style.space(8)
                verticalPadding: Style.space(3)
                bordered: true
                active: (root.content.rotate_minutes || 0) === modelData[0]
                foreground: root.foreground
                fontFamily: root.fontFamily
                enabled: !root.jobRunning
                onClicked: if (!active) root.setContent({ rotate_minutes: modelData[0] },
                  modelData[0] ? "Rotating every " + modelData[1] : "One loop of the chosen effects")
              }
            }
          }

          Row {
            spacing: Style.space(6)

            Button {
              visible: !root.isScreensaver || root.pendingPlaylist !== null
              text: "Play screensaver"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !root.jobRunning
              onClicked: root.play()
            }

            Button {
              visible: root.isScreensaver && root.ss.rotating === true
              text: "Next effect"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !root.working && root.bg.phase !== "rendering"
              onClicked: root.post("/api/screensaver", { action: "next" },
                                   "Writing the next effect; it swaps in when done")
            }
          }
        }

        PanelSeparator {
          visible: root.connected && root.status.presets.length > 0
          foreground: root.foreground
        }

        Column {
          visible: root.connected && root.status.presets.length > 0
          width: parent.width
          spacing: Style.space(8)

          PanelSectionHeader {
            text: "PRESETS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Flow {
            width: parent.width
            spacing: Style.space(6)
            Repeater {
              model: root.connected ? root.status.presets : []
              Button {
                required property var modelData
                text: String(modelData)
                bordered: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                enabled: !root.jobRunning
                onClicked: root.post("/api/presets", { action: "apply", name: String(modelData) },
                                     "Applying " + String(modelData))
              }
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        Button {
          text: "Open full settings"
          bordered: true
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: {
            Quickshell.execDetached(["xdg-open", root.baseUrl + "/"])
            root.close()
          }
        }
      }
    }
  }
}
