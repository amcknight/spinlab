#Requires AutoHotkey v2.0
#SingleInstance Force

global spinlabPID := 0

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

; Ctrl+Alt+C — process passive log into manifest (one-shot, runs and exits)
^!c:: {
    Run 'cmd /c spinlab capture', A_ScriptDir '\..',  'Min'
    Flash "SpinLab — running capture..."
}

; Ctrl+Alt+X — kill everything (orchestrator + Mesen)
^!x:: {
    StopPractice()
    Run 'cmd /c spinlab lua-cmd practice_stop', A_ScriptDir '\..',  'Hide'
    if ProcessExist("Mesen.exe")
        Run 'taskkill /IM Mesen.exe /F',, "Hide"
    Flash "SpinLab — stopped"
}
