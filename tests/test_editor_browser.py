"""Local Edge acceptance on generated media; never downloads a browser."""
from playwright.sync_api import sync_playwright, expect
import pytest

from aivideo.editor_jobs import JobQueue
from aivideo.common import load_json
from test_editor import local_editor, wait_for


@pytest.mark.parametrize("fps", [24, 25, 30, 50, 60])
def test_half_second_controls_repeat_and_persist(local_editor, fps):
    app, address = local_editor
    current = app.store.get(app.store.default_id)
    current["plan"]["settings"]["fps"] = fps
    current["plan"]["shots"][2]["duration_s"] = 1
    step = (fps+1)//2
    current["plan"]["shots"][0].update(start_s=0, end_s=3)
    current["plan"]["shots"][1].update(start_s=0, end_s=3,
                                       audio={"mode":"source","source_id":"проезд.mp4","start_s":0})
    app.store.save(app.store.default_id, current["plan"], current["revision"])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width":1600,"height":1000})
        try:
            page.goto(address)
            pair = page.locator('.transition-card[data-from="s01"][data-to="s02"]')
            expect(pair).to_be_visible()
            pair.click()
            page.locator("#transition-type").select_option("crossfade")
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            initial = round(app.store.get(app.store.default_id)["plan"]["transitions"][0]["duration_s"]*fps)
            field = page.locator("#transition-duration")
            for _ in range(3):
                field.press("ArrowUp")
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            value = app.store.get(app.store.default_id)["plan"]["transitions"][0]["duration_s"]
            assert round(value*fps) == initial+3*step
            for _ in range(3): field.press("ArrowDown")
            plus = page.get_by_role("button",name="Увеличить длительность перехода на 0,5 с",exact=True)
            minus = page.get_by_role("button",name="Уменьшить длительность перехода на 0,5 с",exact=True)
            for _ in range(3): plus.click()
            for _ in range(2): minus.click()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            value = app.store.get(app.store.default_id)["plan"]["transitions"][0]["duration_s"]
            assert round(value*fps) == initial+step
            field.press("ArrowDown")
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            assert round(app.store.get(app.store.default_id)["plan"]["transitions"][0]["duration_s"]*fps)==initial
            page.reload()
            pair.click()
            assert abs(float(field.input_value())-initial/fps)<1e-6
            field.fill(str(1/fps))
            field.press("Tab")
            expect(minus).to_be_disabled()
            for _ in range(3): plus.click()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            assert round(app.store.get(app.store.default_id)["plan"]["transitions"][0]["duration_s"]*fps)==1+3*step
            field.fill(str((3*fps-1)/fps))
            field.press("Tab")
            expect(plus).to_be_disabled()
            for _ in range(3): minus.click()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            assert round(app.store.get(app.store.default_id)["plan"]["transitions"][0]["duration_s"]*fps)==3*fps-1-3*step
            page.locator("#transition-type").select_option("cut")
            expect(plus).to_be_disabled()
            expect(minus).to_be_disabled()
            expect(field).to_be_disabled()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")

            page.locator('.scene-card[data-shot="s01"]').click()
            for _ in range(3): page.get_by_role("button",name="Увеличить начало сцены на 0,5 с",exact=True).click()
            for _ in range(2): page.get_by_role("button",name="Уменьшить начало сцены на 0,5 с",exact=True).click()
            for _ in range(3): page.get_by_role("button",name="Уменьшить конец сцены на 0,5 с",exact=True).click()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            shot = app.store.get(app.store.default_id)["plan"]["shots"][0]
            assert shot["start_s"] == step/fps
            assert shot["end_s"] == (3*fps-3*step)/fps
            page.locator('.scene-card[data-shot="s03"]').click()
            for _ in range(3): page.get_by_role("button",name="Увеличить длительность фото на 0,5 с",exact=True).click()
            page.get_by_role("button",name="Уменьшить длительность фото на 0,5 с",exact=True).click()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            assert app.store.get(app.store.default_id)["plan"]["shots"][2]["duration_s"] == (fps+2*step)/fps
            for width in [1600,1100,400]:
                page.set_viewport_size({"width":width,"height":1000})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                expect(page.get_by_role("button",name="Увеличить длительность фото на 0,5 с",exact=True)).to_be_visible()
            page.reload()
            expect(page.locator(".scene-card")).to_have_count(3)
            assert app.store.get(app.store.default_id)["plan"]["shots"][2]["duration_s"] == (fps+2*step)/fps
        finally:
            browser.close()


def test_transition_editor_in_edge(local_editor, tmp_path):
    app, address = local_editor
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width":1600, "height":1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(address)
            expect(page.locator(".scene-card")).to_have_count(3)
            expect(page.locator(".transition-card")).to_have_count(2)
            pair = page.locator('.transition-card[data-from="s01"][data-to="s02"]')
            pair.click()
            page.locator("#transition-type").select_option("crossfade")
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            expect(page.locator("#duration")).to_contain_text("0:02.00")
            page.locator("#transition-duration").fill("0.24")
            page.locator("#transition-duration").press("Tab")
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            expect(page.locator("#transition-limit")).to_contain_text("7 кадров")
            value = app.store.get(app.store.default_id)
            assert value["plan"]["transitions"][0]["duration_s"] == 7/30
            for width in [1600,1100,400]:
                page.set_viewport_size({"width":width,"height":1000})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                expect(page.locator("#transition-type")).to_be_visible()
                expect(page.locator("#transition-duration")).to_be_visible()
                page.screenshot(path=str(tmp_path/f"transitions-{width}.png"), full_page=True)
            page.set_viewport_size({"width":1600,"height":1000})
            page.locator("#play-transition").click()
            expect(page.locator("#transition-status")).to_contain_text("Просмотр готов", timeout=30000)
            expect(page.locator("#transition-video")).to_be_visible()
            page.locator("#transition-video").evaluate("v=>v.play()")
            page.locator("#transition-duration").fill("1")
            page.locator("#transition-duration").press("Tab")
            expect(page.locator("#save-status")).to_have_text("План сохранён; исправьте ошибки перед сборкой")
            expect(page.locator("#render")).to_be_disabled()
            expect(page.locator("#transition-status")).to_contain_text("устарел")
            page.reload()
            expect(page.locator("#save-status")).to_have_text("План сохранён; исправьте ошибки перед сборкой")
            pair.click()
            page.locator("#transition-duration").fill("0.2")
            page.locator("#transition-duration").press("Tab")
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")

            # Reordering, duplication, removal and catalog insertion break only changed pairs.
            card = page.locator('.scene-card[data-shot="s01"]')
            card.get_by_role("button",name="↓",exact=True).click()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            assert not app.store.get(app.store.default_id)["plan"]["transitions"]
            page.locator("#undo").click()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            assert app.store.get(app.store.default_id)["plan"]["transitions"]
            for action in ["Копия","Удалить"]:
                card.get_by_role("button",name=action,exact=True).click()
                expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
                assert not app.store.get(app.store.default_id)["plan"]["transitions"]
                page.locator("#undo").click()
                expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            card.click()
            page.locator('.catalog-card').first.get_by_role("button",name="Добавить",exact=True).click()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            assert not app.store.get(app.store.default_id)["plan"]["transitions"]
            page.locator("#undo").click()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            pair.click()

            # Hold the sequential worker so pause/resume can be exercised deterministically.
            wait_for(lambda: all(j["status"] not in ("running","queued") for j in app.jobs.list()))
            app.jobs.close()
            app.jobs = JobQueue(app.store, cache=app.jobs.cache)
            page.locator("#transition-type").select_option("fade_black")
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            page.locator("#play-transition").click()
            page.locator("#jobs-tab").click()
            row = page.locator('.job').filter(has_text="Просмотр стыка · В очереди")
            expect(row).to_have_count(1, timeout=10000)
            row.get_by_role("button",name="Пауза",exact=True).click()
            row = page.locator('.job').filter(has_text="Просмотр стыка · На паузе")
            expect(row).to_have_count(1)
            row.get_by_role("button",name="Продолжить",exact=True).click()
            app.jobs.start()
            expect(page.locator("#transition-status")).to_contain_text("Просмотр готов", timeout=30000)
            page.locator("#collapse-global").click()

            page.locator("#new-variant").click()
            page.locator("#variant-name").fill("Переходы · тест")
            page.locator("#create-variant").click()
            expect(page.locator("#variant-dialog")).not_to_be_visible()
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            pair.click()
            page.locator("#transition-type").select_option("cut")
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            page.locator("#variants").select_option(app.store.default_id)
            pair.click()
            expect(page.locator("#transition-type")).to_have_value("fade_black")
            page.locator("#play-transition").click()
            expect(page.locator("#transition-status")).to_contain_text("Просмотр готов")
            # A page closed during a new preview must not cancel the durable job.
            page.locator("#transition-duration").fill("0.3")
            page.locator("#transition-duration").press("Tab")
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            page.locator("#play-transition").click()
            wait_for(lambda: any(j["kind"]=="transition_preview" and j["revision"]==app.store.get(app.store.default_id)["revision"] for j in app.jobs.list()))
            job = next(j for j in app.jobs.list() if j["kind"]=="transition_preview" and j["revision"]==app.store.get(app.store.default_id)["revision"])
            page.close()
            wait_for(lambda: load_json(app.jobs.path(job["id"]))["status"] in ("complete","failed"))
            assert load_json(app.jobs.path(job["id"]))["status"] == "complete"
            assert not errors
        finally:
            browser.close()
