"""Browser checks of the demo app (Playwright, headless Chromium with software WebGL2).

Skipped when Playwright's Chromium is not installed (``uv run playwright install chromium``),
when the demo app cannot be started, or when the browser has no WebGL2. Set
``NNI_DEMO_URL=http://127.0.0.1:3000`` to test an already running demo instead of starting one.
"""

import json

import pytest

pytestmark = pytest.mark.browser
playwright = pytest.importorskip("playwright.sync_api")

SHAPE = (72, 128, 128)
SPACING = (2.5, 1.5, 1.5)
ORIGIN = (-96.0, -96.0, -90.0)


@pytest.fixture(scope="session")
def browser(browser_type, browser_type_launch_args):
    """Skip instead of erroring when Chromium is not installed."""
    try:
        b = browser_type.launch(**browser_type_launch_args)
    except playwright.Error as e:
        pytest.skip(f"Playwright browser not available: {str(e).splitlines()[0]}")
    yield b
    b.close()


@pytest.fixture
def demo(page, demo_url):
    if not page.evaluate("!!document.createElement('canvas').getContext('webgl2')"):
        pytest.skip("this headless Chromium has no WebGL2")
    page.set_viewport_size({"width": 1500, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(demo_url)
    page.wait_for_selector("canvas[data-nni=canvas3d]", timeout=60_000)
    yield page
    assert not errors, errors


def test_demo_mounts_both_canvases_with_webgl2_contexts(demo):
    # Exactly one pair: a StrictMode remount (dev mode) must not leave the first pair behind,
    # nor reuse its lost WebGL contexts ("3D view shader: null").
    assert demo.locator("canvas[data-nni=canvas2d]").count() == 1
    assert demo.locator("canvas[data-nni=canvas3d]").count() == 1
    assert demo.locator(".nni-status.error").count() == 0
    contexts = demo.evaluate(
        """() => {
          const v = document.querySelector('.nni-viewer').nniViewer
          return {
            slices: v.nv2.gl instanceof WebGL2RenderingContext,
            render3d: v.r3.gl instanceof WebGL2RenderingContext,
            lost: [v.nv2.gl.isContextLost(), v.r3.gl.isContextLost()],
          }
        }"""
    )
    assert contexts == {"slices": True, "render3d": True, "lost": [False, False]}
    # The window/level preview filter viewer.js looks up by id exists exactly once.
    assert demo.locator("#wlFilter").count() == 1


def test_load_click_and_read_the_location_in_python_state(demo):
    demo.get_by_role("button", name="Load volume").click()
    demo.wait_for_selector(".nni-viewer[data-nni-ready=true]", timeout=120_000)
    assert demo.locator(".tile-label").all_inner_texts() == ["AXIAL", "CORONAL", "SAGITTAL", "3D"]
    box = demo.locator("canvas[data-nni=canvas2d]").bounding_box()
    assert box is not None
    demo.mouse.click(box["x"] + box["width"] * 0.30, box["y"] + box["height"] * 0.20)
    demo.wait_for_function("document.querySelector('#location')", timeout=15_000)
    loc = json.loads(demo.locator("#location").inner_text())
    a0, a1, a2 = loc["voxel"]
    assert all(0 <= v < n for v, n in zip(loc["voxel"], SHAPE, strict=True))
    expected = [
        a2 * SPACING[2] + ORIGIN[0],
        a1 * SPACING[1] + ORIGIN[1],
        a0 * SPACING[0] + ORIGIN[2],
    ]
    assert loc["mm"] == pytest.approx(expected, abs=0.051)
    assert isinstance(loc["value"], int | float)


def test_layout_prop_hides_the_3d_pane(demo):
    demo.get_by_role("button", name="Load volume").click()
    demo.wait_for_selector(".nni-viewer[data-nni-ready=true]", timeout=120_000)
    demo.locator(".tile-label", has_text="AXIAL").click()  # on_layout_request -> layout="axial"
    demo.wait_for_function(
        "document.querySelector('.nni-pane3d').style.width.startsWith('32')", timeout=15_000
    )
    pane = demo.evaluate("getComputedStyle(document.querySelector('.nni-pane3d')).left")
    assert pane != "0px"


def test_unmounting_destroys_the_viewer(demo):
    lost = demo.evaluate(
        """() => {
          const v = document.querySelector('.nni-viewer').nniViewer
          v.destroy()
          return [v.nv2.gl.isContextLost(), v.r3.gl.isContextLost(), v.ready]
        }"""
    )
    assert lost == [True, True, False]
