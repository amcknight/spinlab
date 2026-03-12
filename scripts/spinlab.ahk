#Requires AutoHotkey v2.0
#SingleInstance Force

global spinlabPID := 0

; Ctrl+Alt+W — toggle SpinLab practice session
^!w:: {
    global spinlabPID
    if (spinlabPID != 0 && ProcessExist(spinlabPID)) {
        ; Session is running — kill it (hard kill; session ended_at will be NULL)
        Run "taskkill /PID " spinlabPID " /F",, "Hide"
        spinlabPID := 0
        ToolTip "SpinLab stopped"
        SetTimer () => ToolTip(), -2000
    } else {
        ; Start a new session in a minimised cmd window
        Run 'cmd /c spinlab practice', '', 'Min', &spinlabPID
        ToolTip "SpinLab started (PID " spinlabPID ")"
        SetTimer () => ToolTip(), -2000
    }
}
