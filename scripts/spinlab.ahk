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

; Ctrl+Alt+W — launch dashboard only (Mesen launches from dashboard UI)
^!w:: {
    global dashPID
    existingPID := (dashPID != 0 && ProcessExist(dashPID)) ? dashPID : FindDashPID()
    if (existingPID != 0) {
        dashPID := existingPID
    } else {
        Run 'spinlab dashboard', A_ScriptDir '\..',  'Min', &dashPID
    }
    Flash("SpinLab started", 2000)
}

; Ctrl+Alt+X — graceful shutdown
^!x:: {
    ; Try graceful HTTP shutdown first
    try {
        RunWait 'cmd /c curl -s -X POST http://localhost:15483/api/shutdown',, 'Hide'
        Sleep 1000
    }
    ; Kill Mesen if running
    if ProcessExist("Mesen.exe")
        Run 'taskkill /IM Mesen.exe /F',, "Hide"
    ; Fallback: kill dashboard if HTTP shutdown didn't work
    StopDashboard()
    Flash "SpinLab — stopped"
}
