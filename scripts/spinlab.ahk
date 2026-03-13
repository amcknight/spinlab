#Requires AutoHotkey v2.0
#SingleInstance Force

global dashPID := 0

Flash(msg, ms := 2000) {
    ToolTip msg
    SetTimer () => ToolTip(), -ms
}

FindDashPID() {
    try {
        tmpFile := A_Temp "\spinlab_port.txt"
        RunWait 'cmd /c "netstat -ano | findstr :15483 | findstr LISTENING > ' tmpFile '"',, "Hide"
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

; Ctrl+Alt+W — launch Mesen + dashboard (idempotent)
^!w:: {
    global dashPID
    ; Launch Mesen if not running
    if !ProcessExist("Mesen.exe") {
        Run 'cmd /c "' A_ScriptDir '\launch.bat"',, 'Hide'
    }
    ; Launch dashboard if not running
    existingPID := (dashPID != 0 && ProcessExist(dashPID)) ? dashPID : FindDashPID()
    if (existingPID != 0) {
        dashPID := existingPID
    } else {
        Run 'spinlab dashboard', A_ScriptDir '\..',  'Min', &dashPID
    }
    Flash("SpinLab started", 2000)
}

; Ctrl+Alt+X — kill everything
^!x:: {
    Run 'cmd /c spinlab lua-cmd practice_stop', A_ScriptDir '\..',  'Hide'
    if ProcessExist("Mesen.exe")
        Run 'taskkill /IM Mesen.exe /F',, "Hide"
    StopDashboard()
    Flash "SpinLab — stopped"
}
