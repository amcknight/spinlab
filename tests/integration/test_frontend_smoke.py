"""Frontend contract smoke: real FE bytes + real backend + fake-loaded game.

Catches FE/BE contract drift (renders reading a dropped field, restructured
shape). No emulator: the TCP/Lua boundary is faked via the
``fake_game_loaded`` fixture (FakeTcpManager with ``connected=True``).

This module overrides the module-scoped ``pytest.mark.emulator`` applied in
``tests/integration/conftest.py`` with ``pytest.mark.frontend`` — these tests
need the built static assets under ``python/spinlab/static/`` but not Mesen.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

pytestmark = pytest.mark.frontend

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

# The index.html nav only exposes three tabs; the "practice" UI lives inside
# the model tab as #practice-card, not as its own tab button.
TABS = ("model", "manage", "segments")


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
