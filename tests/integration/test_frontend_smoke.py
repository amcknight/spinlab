"""Frontend contract smoke: real FE bytes + real backend + fake-loaded game.

Catches FE/BE contract drift (renders reading a dropped field, restructured
shape). The emulator boundary is faked via the ``fake_game_loaded`` fixture
(FakeEmuBackend with ``connected=True``); no live RetroArch is launched.

Requires the built frontend bundle (``cd frontend && npm run build``) and
Playwright Chromium (``playwright install chromium``). Runs as part of the
default suite — no marker needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

pytestmark = []  # not RA-backed; clear emulator marker inherited from integration/conftest.py
# The FastAPI app mounts only /api routes — in production the built bundle is
# served by Vite on :5173 which proxies /api to FastAPI. For this smoke we
# intercept page requests and serve the built assets directly from
# python/spinlab/static/ (precondition: `cd frontend && npm run build`).
_STATIC_ROOT = Path(__file__).resolve().parents[2] / "python" / "spinlab" / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
    ".map":  "application/json",
}


async def _serve_static(route, request):
    """Serve GET / and /static/** from the built frontend on disk.

    All other requests (notably /api/**) fall through to the real FastAPI
    dashboard via route.continue_().
    """
    url_path = request.url.split("://", 1)[-1].split("/", 1)[-1]
    url_path = "/" + url_path.split("?", 1)[0]
    if url_path == "/" or url_path == "":
        fs_path = _STATIC_ROOT / "index.html"
    elif url_path.startswith("/static/"):
        fs_path = _STATIC_ROOT / url_path[len("/static/"):]
    else:
        await route.continue_()
        return
    if not fs_path.is_file():
        await route.fulfill(status=404, body=f"not found: {fs_path}")
        return
    ct = _CONTENT_TYPES.get(fs_path.suffix, "application/octet-stream")
    await route.fulfill(
        status=200, body=fs_path.read_bytes(), headers={"Content-Type": ct},
    )

# The nav exposes four tabs; the "practice" UI lives inside the model tab as
# #practice-card, not as its own tab button. "practice-engine" is the Simulator.
TABS = ("model", "manage", "segments", "practice-engine")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser(fake_dashboard_server, fake_game_loaded):
    # Fail fast with a useful message if the frontend bundle hasn't been built.
    # All 6 tests share this fixture, so the guard fires once per session.
    if not (_STATIC_ROOT / "index.html").exists():
        pytest.fail(
            "Frontend bundle missing. Run `cd frontend && npm run build` first."
        )
    async with async_playwright() as p:
        b = await p.chromium.launch()
        yield b
        await b.close()


@pytest_asyncio.fixture(loop_scope="session")
async def page(browser, fake_dashboard_server):
    # Function-scoped: each test gets a fresh page + empty errors list, so
    # console errors from one test can't leak into another's assertion.
    base_url, _db, _session = fake_dashboard_server
    ctx = await browser.new_context()
    pg = await ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on(
        "console",
        lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None,
    )
    # Route page/asset requests to the on-disk built bundle; /api/** still
    # hits the real FastAPI dashboard via route.continue_().
    await pg.route("**/*", _serve_static)
    await pg.goto(base_url)
    # Don't wait for networkidle — the dashboard holds an SSE stream open,
    # so network never goes idle. DOMContentLoaded + selector waits below
    # are sufficient for contract-level smoke.
    await pg.wait_for_load_state("domcontentloaded")
    # App-ready gate. app.ts's updateFromState() sets the module-level
    # _currentGameId BEFORE it populates #game-name (it's the first line of the
    # same synchronous call). That state arrives asynchronously — via the SSE
    # stream or the initial /api/state fetch — strictly AFTER domcontentloaded.
    # Until it lands, a tab click hits the null-_currentGameId path: the
    # segments tab in particular renders "No game loaded" and returns
    # terminally (segments has no SSE auto-refresh, unlike model/manage), so a
    # click that races the state load leaves the seeded sections unrendered for
    # the full 5s selector timeout. Every test in this module asserts seeded
    # data, so "a game is loaded" is their shared precondition — wait for the
    # game name to appear (the same signal test_sse_delivers_state_update uses)
    # so _currentGameId is guaranteed set before any test clicks a tab. This
    # closes the historical test_segments_tab_lists_seeded_segments flake.
    await pg.wait_for_selector("#game-name:has-text('FakeGame')", timeout=5000)
    yield pg, errors
    await ctx.close()


@pytest.mark.asyncio(loop_scope="session")
async def test_all_tabs_render_without_console_errors(page):
    pg, errors = page
    for tab in TABS:
        await pg.click(f'nav#tabs button.tab[data-tab="{tab}"]')
        # Tab content section becomes .active on click; wait for that instead
        # of networkidle (SSE keeps the network busy indefinitely).
        await pg.wait_for_selector(f"section#tab-{tab}.active", timeout=5000)
    assert not errors, f"console/page errors: {errors}"


@pytest.mark.asyncio(loop_scope="session")
async def test_sse_delivers_state_update(page):
    pg, _errors = page
    # Header's #game-name span is populated from AppState.game_name once the
    # first SSE tick (or the initial /api/state poll) arrives.
    await pg.wait_for_selector("#game-name:has-text('FakeGame')", timeout=5000)


@pytest.mark.asyncio(loop_scope="session")
async def test_practice_card_renders(page):
    pg, _errors = page
    # #practice-card is inside the model tab; it's hidden until a practice
    # session starts, but the element itself must exist in the DOM.
    await pg.click('nav#tabs button.tab[data-tab="model"]')
    assert await pg.locator("#practice-card").count() == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_segments_tab_lists_seeded_segments(page):
    pg, _errors = page
    await pg.click('nav#tabs button.tab[data-tab="segments"]')
    # segments-view.ts renders one <section.segments-level> per distinct
    # level_number. seed_basic_game seeds three distinct levels.
    await pg.wait_for_selector("#segments-view-container section.segments-level", timeout=5000)
    sections = await pg.locator("#segments-view-container section.segments-level").count()
    assert sections >= 1
    # Each section contains a tbody with one <tr> per segment at that level.
    rows = await pg.locator("#segments-view-container section.segments-level tbody tr").count()
    assert rows >= 1
    cold_header_count = await pg.locator(
        "#segments-view-container th", has_text="Cold"
    ).count()
    assert cold_header_count >= 1, "Segments table should render a Cold column"


@pytest.mark.asyncio(loop_scope="session")
async def test_manage_tab_shows_reference(page):
    pg, _errors = page
    await pg.click('nav#tabs button.tab[data-tab="manage"]')
    await pg.wait_for_selector("section#tab-manage.active", timeout=5000)
    # seed_basic_game creates a reference named "FakeRef" and marks it active;
    # manage.ts populates #ref-select with one <option> per reference. The
    # active option's text is "FakeRef " + U+25CF (black circle).
    # <option> elements inside a closed <select> are "hidden" to Playwright's
    # visibility checks — wait for attached state instead.
    await pg.wait_for_selector("#ref-select option", state="attached", timeout=5000)
    option_texts = await pg.locator("#ref-select option").all_inner_texts()
    assert any("FakeRef" in t for t in option_texts), option_texts


@pytest.mark.asyncio(loop_scope="session")
async def test_model_tab_renders_model_table(page):
    pg, _errors = page
    await pg.click('nav#tabs button.tab[data-tab="model"]')
    # model.ts fetchModel() -> updateModel() builds #model-body rows, one per
    # seeded segment. seed_basic_game seeds 3 segments with completed attempts.
    await pg.wait_for_selector("#model-body tr", timeout=5000)
    rows = await pg.locator("#model-body tr").count()
    assert rows >= 1


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def simulator_seeded(fake_dashboard_server, fake_game_loaded):
    """Add em_suite_sampler-backed (gated + ungated) segments with EMPTY
    descriptions so the Simulator has rows whose names come from the endpoint
    structure. Drop any practice engine built before the seed so the next
    /api/practice-engine access rebuilds from the seeded states.
    """
    from tests.factories import seed_sampler_states
    _base_url, db, session = fake_dashboard_server
    ids = seed_sampler_states(db)
    session.get_scheduler()._engine = None  # force lazy rebuild with seeded data
    return ids


@pytest.mark.asyncio(loop_scope="session")
async def test_simulator_tab_renders_segment_names_not_undefined(page, simulator_seeded):
    """Regression: empty-description segments rendered "L… cpundefined → cpundefined"
    because the state payload omitted the endpoint structure. Names must resolve."""
    pg, errors = page
    await pg.click('nav#tabs button.tab[data-tab="practice-engine"]')
    # Panel auto-recomputes on open; gated rows land in the values body. (The
    # cumulative-split table is hidden under the default no_reset policy.)
    await pg.wait_for_selector("#pe-values-body tr", timeout=5000)
    panel_text = await pg.locator("#practice-engine-panel").inner_text()
    assert "undefined" not in panel_text, f"unresolved name fields: {panel_text!r}"
    # Structural formatting actually applied (shortEndpoint start/cp/goal branches).
    assert "L201 start → cp1" in panel_text
    assert "L202 cp1 → goal" in panel_text
    # Ungated segment is listed with its resolved name + gate reason.
    assert "L203 start → goal" in panel_text
    assert not errors, f"console/page errors: {errors}"


@pytest.mark.asyncio(loop_scope="session")
async def test_simulator_recompute_populates_values(page, simulator_seeded):
    """Regression: Recompute 500'd (pool-less states → all-None matrix column).
    With pools rebuilt from events, the value table must populate end-to-end."""
    pg, errors = page
    await pg.click('nav#tabs button.tab[data-tab="practice-engine"]')
    await pg.wait_for_selector("#pe-recompute", timeout=5000)
    await pg.click("#pe-recompute")
    # One row per gated segment appears only if /evaluate returned 200.
    await pg.wait_for_selector("#pe-values-body tr", timeout=5000)
    rows = await pg.locator("#pe-values-body tr").count()
    assert rows >= 1
    assert not errors, f"console/page errors: {errors}"
