#Requires AutoHotkey v2.0
#SingleInstance Force

global spinlabPID := 0
global dashPID := 0

Flash(msg, ms := 2000) {
    ToolTip msg
    SetTimer () => ToolTip(), -ms
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

; Ctrl+Alt+W — launch Mesen (reference mode)
; Ctrl+Alt+P — toggle practice on/off (requires Mesen already running)
^!w:: {
    if ProcessExist("Mesen.exe") {
        Flash "Mesen already running — reference mode active"
    } else {
        Run 'cmd /c "' A_ScriptDir '\launch.bat"',, 'Hide'
        Flash("Launching Mesen2 — reference run mode", 3000)
    }
    ; Start dashboard if not already running
    global dashPID
    if (dashPID != 0 && ProcessExist(dashPID)) {
        Flash("Dashboard already running on :15483", 2000)
    } else {
        Run 'spinlab dashboard', A_ScriptDir '\..',  'Min', &dashPID
        Flash("Dashboard starting on :15483", 2000)
    }
}

^!p:: {
    global spinlabPID
    if StopPractice() {
        Flash "Practice stopped — back to reference mode"
    } else if ProcessExist("Mesen.exe") {
        Run 'spinlab practice', A_ScriptDir '\..',  'Min', &spinlabPID
        Flash "Practice started (PID " spinlabPID ")"
    } else {
        Flash "Launch Mesen first (Ctrl+Alt+W)"
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
    if (dashPID != 0 && ProcessExist(dashPID)) {
        Run "taskkill /PID " dashPID " /T /F",, "Hide"
        dashPID := 0
    }
    Flash "SpinLab — stopped"
}
