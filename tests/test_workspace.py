import copy
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from playwright.sync_api import sync_playwright, expect

from aivideo.common import AnalysisError, ROOT, atomic_json, atomic_text, load_json
from aivideo.handoff import catalog, example_bundle, export_markdown, import_bundle, validate_bundle
from aivideo.projects import ProjectRegistry, ProjectEditorStore
from aivideo.workspace import WorkspaceApp, WorkspaceServer
from aivideo.workspace_jobs import WorkspaceQueue
from test_editor import local_http, wait_for


@pytest.fixture
def managed_project(edit_assets, tmp_path):
    path, original = edit_assets
    registry = ProjectRegistry(tmp_path/"Общее состояние",tmp_path/"Приложение")
    project = registry.create("Поездка · тест",path.parent/"Исходники с пробелами",tmp_path/"Рабочая папка")
    analysis = project.folder("analysis")
    index = load_json(path.parent/"Анализ/media-index.json")
    atomic_json(analysis/"media-index.json",index)
    visual_text = (path.parent/"Анализ/segments.jsonl").read_text(encoding="utf-8")
    atomic_text(analysis/"segments.jsonl",visual_text)
    visual = [json.loads(line) for line in visual_text.splitlines()]
    speech = [{"source_id":"проезд.mp4","segments":[{"start_s":.2,"end_s":.8,"text":"Привет всей семье!","uncertainties":["Шум мотора"]}]}]
    atomic_json(analysis/"transcripts.json",speech)
    atomic_json(analysis/"run.json",{"status":"complete","settings":project.read()["analysis_settings"],
                "media":index["media"],"visual_jobs":[{"key":v["key"]} for v in visual],"asr_jobs":[{"key":"speech"}],"preparation":[]})
    return registry,project


def test_creation_reopen_isolation_and_path_errors(managed_project,tmp_path):
    registry,project = managed_project
    second = registry.create("Другой фильм",project.source,tmp_path/"Второй проект")
    assert project.id != second.id and project.source == second.source
    assert project.folder("cache") != second.folder("cache")
    before = project.path.read_bytes()
    assert registry.open(project.root).id == project.id and project.path.read_bytes()==before
    reopened = ProjectRegistry(registry.root,registry.app_root)
    assert len(reopened.list()) == 2
    with pytest.raises(AnalysisError): registry.create("Повтор",project.source,project.root)
    with pytest.raises(AnalysisError): registry.create("В исходниках",project.source,project.source/"кеш")
    with pytest.raises(AnalysisError): registry.create("В приложении",project.source,registry.app_root/"проект")
    occupied = tmp_path/"Непустая папка"
    occupied.mkdir()
    (occupied/"важный файл.txt").write_text("сохранить",encoding="utf-8")
    with pytest.raises(AnalysisError): registry.create("Непустой",project.source,occupied)
    assert (occupied/"важный файл.txt").read_text(encoding="utf-8")=="сохранить"
    registry.recent["f"*32]={"name":"Отключённый диск","manifest":str(tmp_path/"нет/project.aivideo.json"),"opened_at":"2026"}
    assert not next(p for p in registry.list() if p["id"]=="f"*32)["available"]


def test_complete_md_and_atomic_multi_import(managed_project):
    registry,project = managed_project
    project.update_brief("Подробный семейный фильм и короткий ролик")
    data = catalog(project)
    path,text,revision = export_markdown(project)
    assert path.parent==project.folder("exports")
    assert "Привет всей семье!" in text and "Шум мотора" in text
    assert all(m["id"] in text for m in data["index"]["media"])
    assert revision in text and project.id in text and "fade_black" in text
    assert str(project.root) not in text and str(project.source) not in text
    bundle = example_bundle(project,data)
    bundle["variants"].append(copy.deepcopy(bundle["variants"][0]))
    bundle["variants"][1]["name"]="Короткий фильм"
    checked=validate_bundle(project,bundle)
    assert checked["valid"],checked
    before=project.path.read_bytes()
    bad=copy.deepcopy(bundle)
    bad["variants"][1]["plan"]["shots"][0]["source_id"]="../нет.mp4"
    with pytest.raises(AnalysisError): import_bundle(project,bad)
    assert project.path.read_bytes()==before and not list(project.folder("plans").glob("*.json"))
    imported=import_bundle(project,bundle)
    assert len(imported["variants"])==2
    store=ProjectEditorStore(project)
    for v in store.variants():
        assert not Path(v["path"]).is_absolute()
        assert load_json(store.path(v["id"]))["analysis"]=="../analysis"
    old=set(project.read()["variants"])
    import_bundle(project,bundle)
    assert old < set(project.read()["variants"])


@pytest.mark.parametrize("change",["foreign","stale","range","title","transition","partial","changed_source","rescanned_source","missing_description","missing_speech"])
def test_reject_invalid_handoffs(managed_project,change):
    _,p=managed_project
    bundle=example_bundle(p,catalog(p))
    plan=bundle["variants"][0]["plan"]
    if change=="foreign": bundle["project_id"]="f"*32
    if change=="stale": bundle["catalog_revision"]="old"
    if change=="range": plan["shots"][0]["end_s"]=300
    if change=="title": plan["titles"]=[{"id":"bad","text":"За концом","start_s":0,"end_s":1000}]
    if change=="transition": plan["transitions"]=[{"from_shot_id":"x","to_shot_id":"y","type":"crossfade","duration_s":.5}]
    if change=="partial":
        run=load_json(p.folder("analysis")/"run.json")
        run["status"]="interrupted"
        atomic_json(p.folder("analysis")/"run.json",run)
    if change=="changed_source": (p.source/"дрон.mp4").write_bytes(b"changed")
    if change=="rescanned_source":
        from aivideo.media import scan
        (p.source/"Новый снимок.jpg").write_bytes((p.source/"фото.jpg").read_bytes())
        atomic_json(p.folder("analysis")/"media-index.json",scan(p.source))
    if change=="missing_description":
        file=p.folder("analysis")/"segments.jsonl"
        atomic_text(file,"\n".join(file.read_text(encoding="utf-8").splitlines()[1:]))
    if change=="missing_speech": atomic_json(p.folder("analysis")/"transcripts.json",[])
    assert not validate_bundle(p,bundle)["valid"]


def test_bootstrap_import_without_site_packages(tmp_path):
    code="""
import sys,json,threading,time,http.client
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from aivideo.workspace import WorkspaceApp,WorkspaceServer
base=Path(sys.argv[2]);source=base/'source';source.mkdir()
app=WorkspaceApp(state_root=base/'state',app_root=base/'app',python=base/'missing/python.exe')
server=WorkspaceServer(('127.0.0.1',0),app)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();app.queue.start()
try:
 connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1])
 connection.request('GET','/');response=connection.getresponse();assert response.status==200;assert b'workspace.js' in response.read();connection.close()
 project=app.registry.create('Bootstrap',source,base/'work')
 job=app.queue.enqueue('analyze',project,settings=project.read()['analysis_settings'])
 deadline=time.monotonic()+5
 while time.monotonic()<deadline and app.queue.list()[0]['status']!='failed':time.sleep(.05)
 assert app.queue.list()[0]['status']=='failed'
 assert not any(name in sys.modules for name in ('torch','PIL','transformers','faster_whisper'))
 print('ready')
finally:
 server.shutdown();app.close();server.server_close();thread.join()
"""
    result=subprocess.run([sys.executable,"-S","-c",code,str(ROOT/"src"),str(tmp_path)],capture_output=True,text=True,encoding="utf-8",timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=="ready"


def test_diagnostics_all_checks_without_processing_env(tmp_path):
    from aivideo.installation import diagnose
    app=tmp_path/"Чистое приложение"
    app.mkdir()
    output=app/"diagnostics"
    result=diagnose(output,app)
    checks={r["id"]:r for r in result["checks"]}
    assert checks["runtime"]["status"]=="missing"
    assert checks["ffmpeg"]["status"]=="missing" and checks["ffprobe"]["status"]=="missing"
    assert checks["visual"]["installable"] and checks["asr"]["installable"]
    assert checks["gpu"]["status"]=="blocked" and not checks["nvidia"]["installable"]


def test_install_retry_does_not_reinstall_ready_components(tmp_path,monkeypatch):
    from aivideo import installation
    ready=set()
    calls=[]
    def diagnosis(output,root):
        return {"checks":[{"id":k,"status":"ready" if k in ready else "missing"} for k in ["runtime","cuda","ffmpeg","ffprobe","visual","asr"]]}
    def install_command(args,cwd):
        calls.append([str(a) for a in args])
        if "ffmpeg" in args: raise AnalysisError("Обрыв загрузки")
        ready.update(["runtime","cuda"])
    monkeypatch.setattr(installation,"diagnose",diagnosis)
    monkeypatch.setattr(installation,"run_install",install_command)
    with pytest.raises(AnalysisError): installation.install(["runtime","ffmpeg"],tmp_path,tmp_path)
    with pytest.raises(AnalysisError): installation.install(["runtime","ffmpeg"],tmp_path,tmp_path)
    assert sum("runtime" in c for c in calls)==1
    assert sum("ffmpeg" in c for c in calls)==2


def test_source_selection_is_persistent_and_does_not_delete_originals(managed_project):
    registry, project = managed_project
    source = project.source / "фото.jpg"
    original = source.read_bytes()
    payload = project.select_source("фото.jpg", True)
    assert source.read_bytes() == original
    assert payload["analysis_status"] == "stale"
    assert "фото.jpg" not in {m["id"] for m in payload["index"]["media"]}
    assert payload["index"]["excluded"] == [{"source_id":"фото.jpg"}]
    reopened = ProjectRegistry(registry.root, registry.app_root).get(project.id)
    assert reopened.read()["excluded_sources"] == ["фото.jpg"]
    with pytest.raises(AnalysisError): export_markdown(project)
    with pytest.raises(AnalysisError): project.select_source("../фото.jpg", True)
    project.select_source("фото.jpg", False)
    assert project.read()["excluded_sources"] == []
    assert len(catalog(project)["visual"]) == 3


@pytest.fixture
def workspace_server(managed_project,tmp_path):
    registry,project=managed_project
    app=WorkspaceApp(state_root=registry.root,app_root=registry.app_root,python=sys.executable,
                     proxy_options={"decode":"cpu","encoder":"libx264"})
    server=WorkspaceServer(('127.0.0.1',0),app)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    app.queue.start()
    yield app,f'http://127.0.0.1:{server.server_address[1]}',project.id
    server.shutdown()
    app.close()
    server.server_close()
    thread.join()


def test_scoped_editor_import_render_and_job_paths(workspace_server):
    app,address,key=workspace_server
    project=app.registry.get(key)
    headers={'X-Editor-Token':app.token}
    bundle=example_bundle(project,catalog(project))
    for v in bundle['variants']: v['plan']['settings'].update(width=320,height=180,encoder='libx264',decode='cpu')
    result=local_http.post(address+f'/api/projects/{key}/scenarios/import',json={'bundle':bundle},headers=headers)
    assert result.status_code==200,result.text
    variant=result.json()['default_variant']
    prefix=f'/p/{key}'
    page=local_http.get(address+prefix+'/edit')
    assert page.status_code==200 and 'project-base' in page.text
    plan=local_http.get(address+prefix+'/api/plan').json()
    job=local_http.post(address+prefix+'/api/render',json={'variant':variant,'revision':plan['revision']},headers=headers).json()
    assert not Path(load_json(app.queue.path(job['id']))['output']).is_absolute()
    wait_for(lambda: load_json(app.queue.path(job['id']))['status'] in ('complete','failed'))
    final=load_json(app.queue.path(job['id']))
    assert final['status']=='complete',final
    assert (project.folder('renders')/job['id']/'film.mp4').exists()
    assert not Path(load_json(project.folder('renders')/job['id']/'plan.json')['analysis']).is_absolute()
    movie=local_http.get(address+prefix+'/api/plan').json()['movie']
    assert movie and movie['url'].startswith(prefix+'/media/')
    assert local_http.get(address+movie['url'],headers={'Range':'bytes=0-9'}).status_code==206
    assert local_http.post(address+'/api/projects',json={}).status_code==403
    assert local_http.get(address+'/api/projects',headers={'Host':'example.com'}).status_code==403


def test_source_selection_api_blocks_changes_during_analysis(workspace_server, monkeypatch):
    app, address, key = workspace_server
    headers = {"X-Editor-Token":app.token}
    route = address+f"/api/projects/{key}/source-selection"
    with monkeypatch.context() as patch:
        patch.setattr(app.queue,"list",lambda *args:[{"kind":"analyze","status":"running"}])
        response = local_http.post(route,json={"source_id":"фото.jpg","excluded":True},headers=headers)
        assert response.status_code == 400 and "паузу" in response.json()["error"]
    response = local_http.post(route,json={"source_id":"фото.jpg","excluded":True},headers=headers)
    assert response.status_code == 200
    assert response.json()["index"]["excluded"] == [{"source_id":"фото.jpg"}]


def test_global_queue_restart_and_recovery(managed_project,tmp_path):
    registry,project=managed_project
    second=registry.create("Второй независимый проект",project.source,tmp_path/"Второй рабочий каталог")
    queue=WorkspaceQueue(registry,sys.executable)
    job=queue.enqueue('scan',project)
    other=queue.enqueue('scan',second)
    queue.pause(job['id'])
    assert load_json(queue.path(job['id']))['status']=='paused'
    queue.resume(job['id'])
    queue=WorkspaceQueue(registry,sys.executable)
    assert all(j['status']=='interrupted' for j in queue.list())
    queue.start()
    try:
        assert all(j['status']=='interrupted' for j in queue.list())
        queue.resume(job['id'])
        queue.resume(other['id'])
        wait_for(lambda: load_json(queue.path(job['id']))['status'] in ('complete','failed'))
        assert load_json(queue.path(job['id']))['status']=='complete'
        wait_for(lambda: load_json(queue.path(other['id']))['status'] in ('complete','failed'))
        assert load_json(queue.path(other['id']))['status']=='complete'
        assert load_json(queue.path(job['id']))['finished_at'] <= load_json(queue.path(other['id']))['started_at']
        assert (second.folder('analysis')/'media-index.json').is_file()
        assert not (second.folder('analysis')/'segments.jsonl').exists()
        assert queue.path(other['id']).is_relative_to(second.root)
    finally: queue.close()


def test_workspace_flow_in_edge(workspace_server,tmp_path):
    app,address,key=workspace_server
    p=app.registry.get(key)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page(viewport={'width':1600,'height':1000})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            page.goto(address)
            expect(page.get_by_role('heading',name='Поездка · тест',exact=True)).to_be_visible()
            page.get_by_role('link',name='Открыть проект',exact=True).click()
            expect(page.locator('#analysis-state')).to_contain_text('завершён')
            for width in [1600,1100,400]:
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(tmp_path/f'workspace-{width}.png'),full_page=True)
            page.get_by_role('button',name='Задание для ИИ',exact=True).click()
            page.locator('#brief').fill('Фильм для семьи и короткий ролик о поездке')
            page.locator('#export-handoff').click()
            expect(page.locator('#download-handoff')).to_be_visible()
            with page.expect_download() as download:
                page.locator('#download-handoff').click()
            downloaded=download.value
            md=Path(downloaded.path()).read_text(encoding='utf-8')
            assert 'Привет всей семье!' in md and 'Фильм для семьи' in md
            bundle=example_bundle(p,catalog(p))
            bundle['variants'].append(copy.deepcopy(bundle['variants'][0]))
            bundle['variants'][1]['name']='Короткий ролик'
            page.get_by_role('button',name='Сценарии',exact=True).click()
            page.locator('#scenario-file').set_input_files({'name':'scenario-bundle.json','mimeType':'application/json','buffer':json.dumps(bundle).encode()})
            page.locator('#validate-scenarios').click()
            expect(page.locator('#scenario-errors')).to_contain_text('прошли проверку')
            expect(page.locator('#scenario-summary .project-card')).to_have_count(2)
            page.locator('#import-scenarios').click()
            expect(page.locator('.scene-card')).to_have_count(1)
            expect(page.locator('#variants option')).to_have_count(2)
            assert '/p/'+key+'/edit' in page.url
            page.get_by_role('link',name='Проект',exact=False).first.click()
            expect(page.locator('#page-title')).to_have_text('Поездка · тест')
            assert not errors
        finally: browser.close()


def test_create_open_and_install_controls_in_edge(workspace_server,tmp_path,monkeypatch):
    app,address,key=workspace_server
    original=app.registry.get(key)
    selections=iter([original.source,tmp_path/"Новый фильм с пробелами"])
    monkeypatch.setattr(app,"pick_folder",lambda:{"path":str(next(selections))})
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel="msedge",headless=True)
        page=browser.new_page(viewport={"width":400,"height":1000})
        try:
            page.goto(address)
            page.locator("#new-project").click()
            page.locator("#project-name").fill("Создан через страницу")
            page.locator('[data-pick="source-path"]').click()
            expect(page.locator("#source-path")).to_have_value(str(original.source))
            page.locator('[data-pick="workspace-path"]').click()
            expect(page.locator("#workspace-path")).to_have_value(str(tmp_path/"Новый фильм с пробелами"))
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.locator("#create-project").click()
            expect(page.locator("#page-title")).to_have_text("Создан через страницу")
            page.locator("#scan-project").click()
            expect(page.locator("#source-summary")).to_contain_text("3 файлов")
            page.goto(address)
            page.locator("#open-project").click()
            page.locator("#open-path").fill(str(original.root))
            page.locator("#open-form button.primary").click()
            expect(page.locator("#page-title")).to_have_text(original.read()["name"])
            atomic_json(app.registry.root/"diagnostics/diagnostics.json",{
                "checks":[{"id":"runtime","label":"Python и зависимости","status":"missing","details":"Тестовая отсутствующая среда","install":"runtime","installable":True}],"ready":False})
            submitted=[]
            monkeypatch.setattr(app.queue,"enqueue",lambda kind,**params:submitted.append((kind,params)) or {"id":"0"*32})
            page.goto(address+"/diagnostics")
            expect(page.locator('[data-component="runtime"]')).to_contain_text("Не установлено")
            page.get_by_role("button",name="Установить",exact=True).click()
            expect(page.locator("#processing")).to_have_attribute("open","")
            assert submitted[0][0]=="install" and submitted[0][1]["components"]==["runtime"]
            page.screenshot(path=str(tmp_path/"diagnostics-400.png"),full_page=True)
        finally: browser.close()


def test_unreadable_source_exclusion_and_restore_in_edge(workspace_server,tmp_path,monkeypatch):
    app,address,key=workspace_server
    project=app.registry.get(key)
    broken=project.source/"Повреждённое фото.jpg"
    broken.write_bytes(b"broken photo")
    job=app.queue.enqueue("scan",project)
    wait_for(lambda:load_json(app.queue.path(job["id"]))["status"] in ("complete","failed"))
    assert load_json(app.queue.path(job["id"]))["status"]=="complete"
    submitted=[]
    enqueue=app.queue.enqueue
    def intercept(kind, *args, **kwargs):
        if kind=="analyze":
            submitted.append(kind)
            return {"id":"0"*32}
        return enqueue(kind,*args,**kwargs)
    monkeypatch.setattr(app.queue,"enqueue",intercept)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel="msedge",headless=True)
        page=browser.new_page(viewport={"width":1600,"height":1000})
        try:
            page.goto(address+f"/projects/{key}")
            expect(page.locator("#source-summary")).to_contain_text("ошибок чтения: 1")
            expect(page.locator("#analyze-project")).to_be_enabled()
            page.get_by_role("button",name="Исключить из анализа "+broken.name,exact=True).click()
            expect(page.locator("#excluded-count")).to_have_text("1")
            expect(page.locator("#source-summary")).to_contain_text("ошибок чтения: 0")
            page.reload()
            expect(page.locator("#excluded-count")).to_have_text("1")
            assert broken.read_bytes()==b"broken photo"
            page.locator("#excluded-sources summary").click()
            page.get_by_role("button",name="Вернуть в анализ "+broken.name,exact=True).click()
            expect(page.locator("#source-summary")).to_contain_text("ошибок чтения: 1")
            for width in (1600,1100,400):
                page.set_viewport_size({"width":width,"height":1000})
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                page.screenshot(path=str(tmp_path/f"source-errors-{width}.png"),full_page=True)
            page.locator("#analyze-project").click()
            expect(page.locator("#processing")).to_have_attribute("open","")
            assert submitted==["analyze"]
            assert project.read()["excluded_sources"]==[]
        finally: browser.close()
