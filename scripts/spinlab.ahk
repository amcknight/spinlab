#Requires AutoHotkey v2.0
#SingleInstance Force

global dashPID := 0

Flash(msg, ms := 2000) {
    ToolTip msg
    SetTimer () => ToolTip(), -ms
}

ReadPort(key, fallback) {
    ; Read from .spinlab-ports file (written by dashboard on startup)
    portsFile := A_ScriptDir "\..\\.spinlab-ports"
    if FileExist(portsFile) {
        try {
            for line in StrSplit(FileRead(portsFile), "`n", "`r") {
                parts := StrSplit(line, "=")
                if (parts.Length >= 2 && parts[1] = key)
                    return Integer(parts[2])
            }
        }
    }
    return fallback
}

FindDashPID() {
    port := ReadPort("dashboard_port", 15483)
    try {
        tmpFile := A_Temp "\spinlab_port.txt"
        RunWait 'cmd /c "netstat -ano | findstr :' port ' | findstr LISTENING > ' tmpFile '"',, "Hide"
        line := Trim(FileRead(tmpFile))
        FileDelete tmpFile
        if (line != "") {
            parts := StrSplit(line, " ")
            pid := parts[parts.Length]
            if (pid > 0)
                return Integer(pid)
        }
    }
    return 0
}

StopDashboard() {
    global dashPID
    pid := (dashPID != 0 && ProcessExist(dashPID)) ? dashPID : FindDashPID()
    if (pid != 0 && ProcessExist(pid)) {
        Run "taskkill /PID " pid " /T /F",, "Hide"
        dashPID := 0
        return true
    }
    return false
}

; Ctrl+Alt+W — launch dashboard only (emulator launches from dashboard UI)
^!w:: {
    global dashPID
    existingPID := (dashPID != 0 && ProcessExist(dashPID)) ? dashPID : FindDashPID()
    if (existingPID != 0) {
        dashPID := existingPID
        Flash("SpinLab already running (PID " dashPID ")", 2000)
        return
    }
    ; Pass --config with an absolute path so the dashboard doesn't depend on
    ; CWD. AHK doesn't always inherit the cwd you'd expect, and a relative
    ; "config.yaml" produces a silent FileNotFoundError on detached launches.
    projectDir := A_ScriptDir '\..'
    configPath := projectDir '\config.yaml'
    if !FileExist(configPath) {
        MsgBox("SpinLab: config.yaml not found at`n" configPath
            "`n`nCopy config.example.yaml to config.yaml and edit it,"
            " then try Ctrl+Alt+W again.",
            "SpinLab — startup error", 16)
        return
    }
    ; Run NOT minimized for first-launch shakedown so any error is visible.
    ; Once you're confident it works, change 'Hide' below back to 'Min' if
    ; you want the window minimized.
    Run 'spinlab dashboard --config "' configPath '"', projectDir,, &dashPID
    Flash("SpinLab started — PID " dashPID, 2000)
    ; Sanity-check after 5s: if the process died fast, the dashboard crashed
    ; and the user needs to look at %TEMP%/spinlab-startup-*.log.
    SetTimer () => CheckDashAlive(dashPID), -5000
}

CheckDashAlive(pid) {
    if (pid != 0 && !ProcessExist(pid)) {
        MsgBox("SpinLab dashboard exited within 5s of launch."
            "`nCheck %TEMP%\spinlab-startup-*.log for details."
            "`nOr run 'spinlab dashboard --config <path>' from a terminal"
            " to see the error live.",
            "SpinLab — startup failed", 48)
    }
}

; Ctrl+Alt+X — graceful shutdown
^!x:: {
    port := ReadPort("dashboard_port", 15483)
    ; Try graceful HTTP shutdown first
    try {
        RunWait 'cmd /c curl -s -X POST http://localhost:' port '/api/shutdown',, 'Hide'
        Sleep 1000
    }
    ; Kill Mesen if running
    if ProcessExist("Mesen.exe")
        Run 'taskkill /IM Mesen.exe /F',, "Hide"
    ; Fallback: kill dashboard if HTTP shutdown didn't work
    StopDashboard()
    Flash "SpinLab — stopped"
}
