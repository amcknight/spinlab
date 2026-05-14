# SpinLab Diagrams

Living collection of architecture and flow diagrams. Each section is independent — add new ones as `## <Title>` blocks. The authoritative prose lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); this file is for the visual cross-references.

## High-Level Architecture

```mermaid
flowchart TB
    subgraph FE["Frontend — TypeScript + Vite (:5173 dev)"]
        UI["Browser UI<br/>frontend/src"]
    end

    subgraph BE["Dashboard — FastAPI (:15483)"]
        Routes["Routes<br/>routes/*"]
        SM["SessionManager<br/>route_event()"]
        RC["ReferenceController<br/>IDLE / RECORDING / PAUSED"]
        PS["PracticeSession<br/>pick → load → wait → log"]
        CF["ColdFillController"]
        SRT["SpeedRunTiming"]
        SRec["SegmentRecorder<br/>pairs events → segments"]
        Sched["Scheduler<br/>estimators + allocators"]
    end

    subgraph RAL["RetroArch integration (retroarch/*)"]
        Orch["RetroArchOrchestrator"]
        NCI["NCIClient<br/>UDP transport"]
        Poll["Poller<br/>60 Hz WRAM reads"]
        Det["TransitionDetector<br/>predicates.py"]
        SIO["StateIO<br/>.mss save/load"]
        Mov["MovieRecorder / Player<br/>BSV replay"]
    end

    DB[("SQLite<br/>spinlab.db")]
    RA["RetroArch + snes9x_libretro<br/>NCI on :55355"]
    Files[/"spinlab_state_dir<br/>*.mss, *.replay"/]

    UI -- "HTTP + SSE /api/events" --> Routes
    Routes --> SM

    SM --> RC
    SM --> PS
    SM --> CF
    SM --> SRT

    RC --> SRec
    SRec --> DB
    RC --> DB
    PS --> Sched
    Sched --> DB

    RC -. drives .-> Orch
    PS -. drives .-> Orch
    CF -. drives .-> Orch

    Orch --> NCI
    Orch --> Poll
    Orch --> SIO
    Orch --> Mov

    Poll --> Det
    Det -- "typed events<br/>LevelEntrance, Death, …" --> SM

    NCI <-- "UDP NCI" --> RA
    SIO <--> Files
    Mov <--> Files
```

Notes / deliberate simplifications:

- The poller actually owns two detectors (`TransitionDetector` + `ColdFillSpawnDetector`); the cold-fill one is folded into `ColdFillController` here since that's the consumer.
- `Poller`, `StateIO`, and `MovieRecorder` all funnel through `NCIClient` — only the orchestrator-owned edges are drawn to keep the picture readable.
- The reference state machine (IDLE/RECORDING/PAUSED) is shown only as a label; a proper rendering would be a separate `stateDiagram-v2`.
