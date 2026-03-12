#Requires AutoHotkey v2.0
#SingleInstance Force

global spinlabPID := 0

; Ctrl+Alt+W — three-state cycle:
;   idle  →  reference run (launch Mesen, passive recorder)
;   reference run  →  practice mode (start orchestrator)
;   practice mode  →  reference run (stop orchestrator, Mesen stays open)
^!w:: {
    global spinlabPID

    mesenRunning    := ProcessExist("Mesen.exe")
    practiceRunning := (spinlabPID != 0 && ProcessExist(spinlabPID))

    if (practiceRunning) {
        ; Practice → stop orchestrator, clear Lua practice state, reset game
        Run "taskkill /PID " spinlabPID " /T /F",, "Hide"
        spinlabPID := 0
        ; Lua detects the dropped connection and auto-clears practice + resets game
        ToolTip "Practice stopped — resetting to reference run"
        SetTimer () => ToolTip(), -2000

    } else if (mesenRunning) {
        ; Reference run → start practice session
        Run 'cmd /c spinlab practice', A_ScriptDir '\..',  'Min', &spinlabPID
        ToolTip "Practice started (PID " spinlabPID ")"
        SetTimer () => ToolTip(), -2000

    } else {
        ; Idle → launch Mesen2 with ROM and Lua script
        Run 'cmd /c "' A_ScriptDir '\launch.bat"',, 'Hide'
        ToolTip "Launching Mesen2 — reference run mode"
        SetTimer () => ToolTip(), -3000
    }
}

; Ctrl+Alt+C — process passive log into manifest (one-shot, runs and exits)
^!c:: {
    Run 'cmd /c spinlab capture', A_ScriptDir '\..',  'Min'
    ToolTip "SpinLab — running capture..."
    SetTimer () => ToolTip(), -2000
}

; Ctrl+Alt+X — kill everything (orchestrator + Mesen)
^!x:: {
    global spinlabPID
    if (spinlabPID != 0 && ProcessExist(spinlabPID)) {
        Run "taskkill /PID " spinlabPID " /T /F",, "Hide"
        spinlabPID := 0
    }
    Run 'cmd /c spinlab lua-cmd practice_stop', A_ScriptDir '\..',  'Hide'
    if ProcessExist("Mesen.exe") {
        Run 'taskkill /IM Mesen.exe /F',, "Hide"
    }
    ToolTip "SpinLab — stopped"
    SetTimer () => ToolTip(), -2000
}
