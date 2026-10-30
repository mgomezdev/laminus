"""Dashboard e2e: every assertion goes through the browser against the real app."""
import re

import pytest
from playwright.sync_api import Page, expect

from e2e.conftest import FILAMENT, MACHINE, PROCESS

FAILPLATE_LABEL = "Slice Plate 2"  # mock slicer fails plate 2 (see conftest)

# Mock slices finish in well under a second, except the slow plate (SLOW_SECONDS).
expect.set_options(timeout=15_000)


@pytest.fixture
def dash(page: Page, server):
    page.goto(server["url"])
    expect(page.locator("#machine-select option", has_text=MACHINE)).to_have_count(1)
    return page


def choose_profiles(page: Page, plate="Slice Plate 1"):
    page.select_option("#machine-select", label=MACHINE)
    page.select_option("#process-select", label=PROCESS)
    page.select_option("#filament-select", label=FILAMENT)
    page.select_option("#plate-select", label=plate)


def job_row(page: Page, job_id: str):
    return page.locator(f'#jobs-tbody tr[data-job-id="{job_id}"]')


# ---- slice form ------------------------------------------------------------------

def test_profiles_populate_dropdowns(dash: Page):
    expect(dash.locator("#process-select option", has_text=PROCESS)).to_have_count(1)
    expect(dash.locator("#filament-select option", has_text=FILAMENT)).to_have_count(1)
    expect(dash.locator("#no-presets-warning")).to_be_hidden()
    expect(dash.locator("#api-status")).to_contain_text("Connected")
    expect(dash.locator("#plate-select")).to_have_value("1")


def test_phone_width_has_no_horizontal_scroll(dash: Page, api):
    api.wait_for(api.create_job(), "completed")
    dash.set_viewport_size({"width": 390, "height": 844})
    dash.click("#jobs-refresh-btn")
    expect(dash.locator("#jobs-tbody tr[data-job-id]")).to_have_count(1)
    assert dash.evaluate("document.documentElement.scrollWidth") <= 390


def test_rejects_unsupported_file_type(dash: Page, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    dash.set_input_files("#file-input", str(bad))
    expect(dash.locator("#console-output")).to_contain_text("Invalid file format: .txt")
    expect(dash.locator("#submit-btn")).to_be_disabled()


def test_select_and_remove_file(dash: Page, stl_file):
    dash.set_input_files("#file-input", str(stl_file))
    expect(dash.locator("#selected-file-name")).to_have_text("part.stl")
    expect(dash.locator("#dropzone")).to_be_hidden()
    expect(dash.locator("#submit-btn")).to_be_enabled()
    dash.click("#file-remove")
    expect(dash.locator("#dropzone")).to_be_visible()
    expect(dash.locator("#submit-btn")).to_be_disabled()
    expect(dash.locator("#console-output")).to_contain_text("File removed")


def test_submit_without_profiles_fails_fast(dash: Page, stl_file):
    dash.set_input_files("#file-input", str(stl_file))
    dash.click("#submit-btn")
    expect(dash.locator("#error-card")).to_be_visible()
    expect(dash.locator("#error-desc")).to_contain_text("Select a printer, process, and filament")
    expect(dash.locator("#submit-btn")).to_be_enabled()


def test_slice_success_streams_logs_and_downloads(dash: Page, stl_file):
    dash.set_input_files("#file-input", str(stl_file))
    choose_profiles(dash)
    dash.click("#submit-btn")
    expect(dash.locator("#success-card")).to_be_visible()
    console = dash.locator("#console-output")
    expect(console).to_contain_text("Slicing job initialized with ID")
    expect(console).to_contain_text("Starting slice: prepared.3mf")
    expect(console).to_contain_text("Output: plate_1.gcode")

    job_id = re.search(r"ID: (\S+)", console.inner_text()).group(1)
    row = job_row(dash, job_id)
    expect(row.locator(".status-pill")).to_have_text("completed")
    expect(row).to_contain_text("part.stl")
    expect(row).to_contain_text("plate_1.gcode")

    with dash.expect_download() as dl:
        dash.click("#download-btn")
    assert dl.value.suggested_filename.endswith(".gcode")

    # The artifact is one-time: once downloaded the row says so and loses its link.
    dash.click("#jobs-refresh-btn")
    expect(row).to_contain_text("(downloaded)")
    expect(row.locator(".jobs-download")).to_have_count(0)


def test_slice_failure_shows_error(dash: Page, stl_file):
    dash.set_input_files("#file-input", str(stl_file))
    choose_profiles(dash, plate=FAILPLATE_LABEL)
    dash.click("#submit-btn")
    expect(dash.locator("#error-card")).to_be_visible()
    expect(dash.locator("#error-desc")).to_contain_text("OrcaSlicer returned non-zero")
    expect(dash.locator("#console-output")).to_contain_text("mock: ERROR slicing failed")
    row = dash.locator("#jobs-tbody tr").first
    expect(row.locator(".status-pill")).to_have_text("failed")
    expect(row).to_contain_text("OrcaSlicer slice process failed")
    expect(row.locator(".jobs-download")).to_have_count(0)


# ---- job history -------------------------------------------------------------------

def test_empty_history(dash: Page):
    expect(dash.locator("#jobs-tbody")).to_contain_text("No jobs yet")


def test_manual_refresh_picks_up_external_jobs(dash: Page, api):
    dash.uncheck("#jobs-auto-refresh")
    job_id = api.create_job()
    api.wait_for(job_id, "completed")
    expect(dash.locator("#jobs-tbody")).to_contain_text("No jobs yet")
    dash.click("#jobs-refresh-btn")
    expect(job_row(dash, job_id).locator(".status-pill")).to_have_text("completed")


def test_auto_refresh_picks_up_external_jobs(dash: Page, api):
    job_id = api.create_job()
    expect(job_row(dash, job_id).locator(".status-pill")).to_have_text("completed")


def test_status_filter(dash: Page, api):
    ok = api.create_job()
    api.wait_for(ok, "completed")
    bad = api.create_job(plate="2")
    api.wait_for(bad, "failed")
    dash.click("#jobs-refresh-btn")
    expect(dash.locator("#jobs-tbody tr")).to_have_count(2)

    dash.select_option("#jobs-status-filter", "failed")
    expect(dash.locator("#jobs-tbody tr")).to_have_count(1)
    expect(job_row(dash, bad)).to_be_visible()

    dash.select_option("#jobs-status-filter", "completed")
    expect(job_row(dash, ok)).to_be_visible()
    expect(dash.locator("#jobs-tbody tr")).to_have_count(1)

    dash.select_option("#jobs-status-filter", "pending")
    expect(dash.locator("#jobs-tbody")).to_contain_text("No pending jobs.")


# ---- queue ---------------------------------------------------------------------------

def test_queue_tracks_active_job_and_full_slots(dash: Page, api, page: Page, server, stl_file):
    expect(dash.locator("#queue-list")).to_contain_text("Idle")
    expect(dash.locator("#queue-slots")).to_have_text("0 / 1")

    job_id = api.create_job(plate="3")
    item = dash.locator(f'#queue-list [data-job-id="{job_id}"]')
    expect(item.locator(".status-pill")).to_have_text("slicing")
    expect(item).to_contain_text("part.stl")
    expect(dash.locator("#queue-slots")).to_have_text("1 / 1")
    expect(dash.locator("#queue-bar")).to_have_class(re.compile(r"\bfull\b"))

    # With every slot taken, a slice submitted from another tab is turned away.
    other = page.context.new_page()
    other.goto(server["url"])
    expect(other.locator("#machine-select option", has_text=MACHINE)).to_have_count(1)
    other.set_input_files("#file-input", str(stl_file))
    choose_profiles(other)
    other.click("#submit-btn")
    expect(other.locator("#error-desc")).to_contain_text("Too many active jobs (1/1)")

    api.wait_for(job_id, "completed")
    expect(dash.locator("#queue-list")).to_contain_text("Idle")
    expect(dash.locator("#queue-slots")).to_have_text("0 / 1")


# ---- log viewer ------------------------------------------------------------------------

def test_log_viewer_streams_live_job(dash: Page, api):
    job_id = api.create_job(plate="3")
    dash.locator(f'#queue-list [data-job-id="{job_id}"] [data-logs]').click()
    viewer = dash.locator("#log-viewer")
    expect(viewer).to_be_visible()
    expect(dash.locator("#log-viewer-title")).to_have_text(f"Job {job_id[:8]}")
    expect(dash.locator("#log-viewer-file")).to_have_text("part.stl")
    body = dash.locator("#log-viewer-body")
    expect(body).to_contain_text("mock: slicing step 1/3")
    expect(dash.locator("#log-viewer-status .status-pill")).to_have_text("slicing")
    expect(body).to_contain_text("[SYSTEM] Job completed.")
    expect(dash.locator("#log-viewer-status .status-pill")).to_have_text("completed")
    assert dash.url.endswith(f"#job={job_id}")


def test_log_viewer_for_failed_job_from_history(dash: Page, api):
    job_id = api.create_job(plate="2")
    api.wait_for(job_id, "failed")
    job_row(dash, job_id).locator("[data-logs]").click()
    body = dash.locator("#log-viewer-body")
    expect(body).to_contain_text("mock: ERROR slicing failed")
    expect(body.locator(".console-line.error").last).to_contain_text("Job failed: OrcaSlicer returned non-zero")


def test_log_viewer_deep_link_and_close(page: Page, server, api):
    job_id = api.create_job()
    api.wait_for(job_id, "completed")
    page.goto(f"{server['url']}/#job={job_id}")
    expect(page.locator("#log-viewer")).to_be_visible()
    expect(page.locator("#log-viewer-body")).to_contain_text("Output: plate_1.gcode")

    page.keyboard.press("Escape")
    expect(page.locator("#log-viewer")).to_be_hidden()
    assert "#job=" not in page.url

    job_row(page, job_id).locator("[data-logs]").click()
    expect(page.locator("#log-viewer")).to_be_visible()
    page.click("#log-viewer-close")
    expect(page.locator("#log-viewer")).to_be_hidden()


def test_log_viewer_after_download_says_logs_are_gone(dash: Page, api):
    job_id = api.create_job()
    api.wait_for(job_id, "completed")
    api.download(job_id)
    job_row(dash, job_id).locator("[data-logs]").click()
    expect(dash.locator("#log-viewer-body")).to_contain_text("Logs are no longer available")


# ---- disk cleanup ----------------------------------------------------------------------

def test_cleanup_scan_and_run(dash: Page, server):
    expect(dash.locator("#cleanup-preview")).to_have_text("Nothing to clean up right now.")
    expect(dash.locator("#cleanup-run-btn")).to_be_disabled()

    orphan = server["jobs_dir"] / "orphaned-job"
    orphan.mkdir()
    (orphan / "input.stl").write_bytes(b"x" * 2048)

    dash.select_option("#cleanup-age", "0")
    dash.click("#cleanup-scan-btn")
    expect(dash.locator("#cleanup-preview")).to_contain_text("1 stale item(s) found")
    dash.once("dialog", lambda d: d.accept())
    dash.click("#cleanup-run-btn")
    expect(dash.locator("#cleanup-preview")).to_contain_text("Removed 1 item(s)")
    assert not orphan.exists()
