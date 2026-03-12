#Requires AutoHotkey v2.0
#SingleInstance Force

global spinlabPID := 0
global dashPID := 0

Flash(msg, ms := 2000) {
    ToolTip msg
    SetTimer () => ToolTip(), -ms
}

FindDashPID() {
    ; Find PID listening on port 15483 via netstat
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

StopPractice() {
    global spinlabPID
    if (spinlabPID != 0 && ProcessExist(spinlabPID)) {
        Run "taskkill /PID " spinlabPID " /T /F",, "Hide"
        spinlabPID := 0
        return true
    }
    return false
}

; Ctrl+Alt+W — three-state cycle:
;   idle  →  reference run (launch Mesen, passive recorder)
;   reference run  →  practice mode (start orchestrator)
;   practice mode  →  reference run (stop orchestrator, Mesen stays open)
^!w:: {
    global spinlabPID
    if StopPractice() {
        ; Lua detects the dropped connection and auto-clears practice + resets game
        Flash "Practice stopped — resetting to reference run"
    } else if ProcessExist("Mesen.exe") {
        Run 'spinlab practice', A_ScriptDir '\..',  'Min', &spinlabPID
        Flash "Practice started (PID " spinlabPID ")"
    } else {
        Run 'cmd /c "' A_ScriptDir '\launch.bat"',, 'Hide'
        Flash("Launching Mesen2 — reference run mode", 3000)
    }
}

; Ctrl+Alt+D — start/check dashboard
^!d:: {
    global dashPID
    existingPID := (dashPID != 0 && ProcessExist(dashPID)) ? dashPID : FindDashPID()
    if (existingPID != 0) {
        dashPID := existingPID
        Flash("Dashboard already running on :15483", 2000)
    } else {
        Run 'spinlab dashboard', A_ScriptDir '\..',  'Min', &dashPID
        Flash("Dashboard starting on :15483", 2000)
    }
}

; Ctrl+Alt+C — process passive log into manifest (one-shot, runs and exits)
^!c:: {
    Run 'cmd /c spinlab capture', A_ScriptDir '\..',  'Min'
    Flash "SpinLab — running capture..."
}

; Ctrl+Alt+X — kill everything (orchestrator + Mesen + dashboard)
^!x:: {
    global dashPID
    StopPractice()
    Run 'cmd /c spinlab lua-cmd practice_stop', A_ScriptDir '\..',  'Hide'
    if ProcessExist("Mesen.exe")
        Run 'taskkill /IM Mesen.exe /F',, "Hide"
    StopDashboard()
    Flash "SpinLab — stopped"
}
