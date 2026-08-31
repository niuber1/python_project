import json
import threading

import pytest
from pydantic import ValidationError

from crawler_tool.config import TASKS, Settings
from crawler_tool.engine import RunManager
from crawler_tool.events import EventStore
from crawler_tool.models import KmsResult, PolicyArticle, PolicyCandidate, PushArticlesRequest, StartRunRequest


class FakeDb:
    def __init__(self): self.items=[]; self.article_results=[]; self.run_updates=[]
    def create_run_item(self, **values): self.items.append(values)
    def update_run_item(self, item_id, **values): self.items.append({"id":item_id, **values})
    def update_article_kms(self, article_id, result): self.article_results.append((article_id,result))
    def insert_article(self, *args, **kwargs): return True
    def update_run(self, run_id, **values): self.run_updates.append(values)
    def create_run(self, run_id, trigger_type, task_codes, dry_run): self.run_updates.append({"id": run_id, "trigger": trigger_type, "codes": task_codes, "dry_run": dry_run})
    def refresh_existing_article(self, *args): self.refreshed = args


class NeverFetch:
    def fetch(self, _): raise AssertionError("已存在失败记录不应重新抓网页")


class FakeKms:
    def push(self, payload): return KmsResult(success=True, code="1", message="ok")
    def close(self): pass


def test_existing_failed_article_only_repushes_saved_payload():
    db=FakeDb(); manager=RunManager(Settings(_env_file=None),db,EventStore())
    saved={"id":"c"*32,"bt":"标题","url":"https://example.com","content":"<p>正文</p>","source":"来源","baseId":"base"}
    existing={"policy_crawler_article_id":"article","kms_status":"failed","kms_result_code":"4","kms_payload_json":json.dumps(saved)}
    candidate=PolicyCandidate(source_code="qifuyun",source_item_id="q1",project_name="项目",detail_ref="q1")
    outcome=manager._process_candidate("run","qifuyun_declare",TASKS["qifuyun_declare"],candidate,existing,False,NeverFetch(),FakeKms())
    assert outcome=="succeeded" and db.article_results[0][0]=="article"


def test_crawl_phase_does_not_call_kms():
    db=FakeDb(); manager=RunManager(Settings(_env_file=None),db,EventStore())
    candidate=PolicyCandidate(source_code="qifuyun",source_item_id="q1",project_name="项目",detail_ref="q1")
    class FakeAdapter:
        def fetch(self,_):
            return PolicyArticle(source_code="qifuyun",source_item_id="q1",source_name="源",title="标题",project_name="项目",original_url="https://example.com",raw_content_html="<p>正文</p>")
    class BoomKms:
        def push(self,payload): raise AssertionError("仅抓取模式不应调用 KMS")
    outcome=manager._process_candidate("run","qifuyun_declare",TASKS["qifuyun_declare"],candidate,None,False,FakeAdapter(),BoomKms(),push_kms=False)
    assert outcome=="succeeded" and db.article_results==[]


def test_crawl_phase_skips_existing_article():
    db=FakeDb(); manager=RunManager(Settings(_env_file=None),db,EventStore())
    existing={"policy_crawler_article_id":"article","kms_status":"failed","kms_result_code":"4"}
    candidate=PolicyCandidate(source_code="qifuyun",source_item_id="q1",project_name="项目",detail_ref="q1")
    outcome=manager._process_candidate("run","qifuyun_declare",TASKS["qifuyun_declare"],candidate,existing,False,NeverFetch(),FakeKms(),push_kms=False)
    assert outcome=="skipped"


def test_refresh_existing_only_marks_changed_document_for_manual_update():
    db=FakeDb(); manager=RunManager(Settings(_env_file=None),db,EventStore())
    candidate=PolicyCandidate(source_code="qifuyun",source_item_id="q1",project_name="项目",detail_ref="q1")
    existing={"policy_crawler_article_id":"article","kms_status":"success","content_hash":"old"}
    class Adapter:
        def fetch(self,_):
            return PolicyArticle(source_code="qifuyun",source_item_id="q1",source_name="源",title="标题",project_name="项目",original_url="https://example.com",raw_content_html="<p>最新正文</p>")
    outcome=manager._process_candidate("run","qifuyun_declare",TASKS["qifuyun_declare"],candidate,existing,False,Adapter(),None,push_kms=False,refresh_existing=True)
    assert outcome=="succeeded" and db.refreshed[-1] is True


def test_refresh_existing_skips_when_content_hash_unchanged():
    from crawler_tool.html_utils import compose_document_content, content_sha256
    db=FakeDb(); manager=RunManager(Settings(_env_file=None),db,EventStore())
    candidate=PolicyCandidate(source_code="qifuyun",source_item_id="q1",project_name="项目",detail_ref="q1")
    article=PolicyArticle(source_code="qifuyun",source_item_id="q1",source_name="源",title="标题",project_name="项目",original_url="https://example.com",raw_content_html="<p>正文</p>")
    existing={"policy_crawler_article_id":"article","kms_status":"success","content_hash":content_sha256(compose_document_content(article))}
    outcome=manager._process_candidate("run","qifuyun_declare",TASKS["qifuyun_declare"],candidate,existing,False,type("Adapter",(),{"fetch":lambda self,_:article})(),None,push_kms=False,refresh_existing=True)
    assert outcome=="skipped" and not hasattr(db,"refreshed")


def test_push_phase_pushes_pending_rows(monkeypatch):
    import crawler_tool.engine as engine_mod
    db=FakeDb()
    saved={"id":"c"*32,"bt":"标题","url":"https://example.com","content":"<p>正文</p>","source":"来源","baseId":"base"}
    db.list_pending_articles=lambda source_codes=None:[{
        "policy_crawler_article_id":"a1","source_code":"qifuyun","source_item_id":"q1",
        "kms_document_id":"d"*32,"base_id":"base","title":"标题","kms_payload_json":json.dumps(saved)}]
    monkeypatch.setattr(engine_mod,"KmsClient",lambda settings: FakeKms())
    manager=RunManager(Settings(_env_file=None),db,EventStore())
    manager._stop_flags["run"]=threading.Event(); manager._active_run="run"
    manager._execute_push("run",["qifuyun_declare"],False)
    assert db.article_results[0][0]=="a1" and db.article_results[0][1].success
    assert manager._active_run is None


def test_push_phase_dry_run_does_not_call_kms(monkeypatch):
    import crawler_tool.engine as engine_mod
    db=FakeDb()
    saved={"id":"c"*32,"bt":"标题","url":"https://example.com","content":"<p>正文</p>","source":"来源","baseId":"base"}
    db.list_pending_articles=lambda source_codes=None:[{
        "policy_crawler_article_id":"a1","source_code":"qifuyun","source_item_id":"q1",
        "kms_document_id":"d"*32,"base_id":"base","title":"标题","kms_payload_json":json.dumps(saved)}]
    def boom(_): raise AssertionError("预检不应构造 KmsClient")
    monkeypatch.setattr(engine_mod,"KmsClient",boom)
    manager=RunManager(Settings(_env_file=None),db,EventStore())
    manager._stop_flags["run"]=threading.Event(); manager._active_run="run"
    manager._execute_push("run",["qifuyun_declare"],True)
    assert db.article_results==[] and manager._active_run is None


def test_start_run_request_phase_validation():
    assert StartRunRequest(task_codes=[], phase="crawl").phase == "crawl"
    assert StartRunRequest(task_codes=[]).phase == "all"
    assert StartRunRequest(task_codes=[], refresh_existing=True).refresh_existing
    with pytest.raises(ValidationError):
        StartRunRequest(task_codes=[], phase="bogus")


def test_auto_sync_chains_push_run(monkeypatch):
    import crawler_tool.engine as engine_mod
    db = FakeDb()
    db.list_run_article_ids = lambda run_id: ["a1", "a2"]
    saved = {"id": "c" * 32, "bt": "标题", "url": "https://example.com", "content": "<p>正文</p>", "source": "来源", "baseId": "base"}
    db.get_articles_by_ids = lambda ids: [{
        "policy_crawler_article_id": i, "source_code": "qifuyun", "source_item_id": i,
        "kms_document_id": "d" * 32, "base_id": "base", "title": f"标题{i}", "kms_payload_json": json.dumps(saved)} for i in ids]
    monkeypatch.setattr(engine_mod, "KmsClient", lambda settings: FakeKms())
    monkeypatch.setattr(engine_mod.kms_kb, "title_exists_in_base", lambda settings, title, base_id: False)
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    manager._stop_flags["crawl"] = threading.Event(); manager._active_run = "crawl"
    push_id = manager._start_auto_push("crawl")
    assert push_id is not None and push_id != "crawl"
    assert manager._active_run == push_id
    import time as _t
    for _ in range(100):
        if len(db.article_results) >= 2 and manager._active_run is None:
            break
        _t.sleep(0.02)
    assert {aid for aid, _ in db.article_results} == {"a1", "a2"}
    assert manager._active_run is None  # 推送批次完成后释放


def test_auto_sync_no_articles_returns_none():
    db = FakeDb()
    db.list_run_article_ids = lambda run_id: []
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    manager._stop_flags["crawl"] = threading.Event(); manager._active_run = "crawl"
    assert manager._start_auto_push("crawl") is None


def test_push_articles_selected_only_and_dedup(monkeypatch):
    import crawler_tool.engine as engine_mod
    db = FakeDb()
    saved = {"id": "c" * 32, "bt": "标题", "url": "https://example.com", "content": "<p>正文</p>", "source": "来源", "baseId": "base"}
    rows = [
        {"policy_crawler_article_id": "a1", "source_code": "qifuyun", "source_item_id": "q1", "kms_document_id": "d" * 32, "base_id": "base", "title": "标题1", "kms_payload_json": json.dumps(saved)},
        {"policy_crawler_article_id": "a2", "source_code": "suishenban", "source_item_id": "s1", "kms_document_id": "e" * 32, "base_id": "base", "title": "标题2", "kms_payload_json": json.dumps(saved)},
    ]
    db.get_articles_by_ids = lambda ids: [r for r in rows if r["policy_crawler_article_id"] in ids]
    monkeypatch.setattr(engine_mod, "KmsClient", lambda settings: FakeKms())
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    manager._stop_flags["run"] = threading.Event(); manager._active_run = "run"
    manager._execute_push_ids("run", ["a2", "a1", "a2"], False)
    assert {aid for aid, _ in db.article_results} == {"a1", "a2"}
    assert len(db.article_results) == 2 and manager._active_run is None


def test_push_articles_dry_run_does_not_call_kms(monkeypatch):
    import crawler_tool.engine as engine_mod
    db = FakeDb()
    saved = {"id": "c" * 32, "bt": "标题", "url": "https://example.com", "content": "<p>正文</p>", "source": "来源", "baseId": "base"}
    db.get_articles_by_ids = lambda ids: [{
        "policy_crawler_article_id": "a1", "source_code": "qifuyun", "source_item_id": "q1",
        "kms_document_id": "d" * 32, "base_id": "base", "title": "标题", "kms_payload_json": json.dumps(saved)}]
    def boom(_): raise AssertionError("预检不应构造 KmsClient")
    monkeypatch.setattr(engine_mod, "KmsClient", boom)
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    manager._stop_flags["run"] = threading.Event(); manager._active_run = "run"
    manager._execute_push_ids("run", ["a1"], True)
    assert db.article_results == [] and manager._active_run is None


def test_push_marks_synced_when_title_exists_in_kms_kb(monkeypatch):
    import crawler_tool.engine as engine_mod
    db = FakeDb()
    saved = {"id": "c" * 32, "bt": "已存在标题", "url": "https://example.com", "content": "<p>正文</p>", "source": "来源", "baseId": "base"}
    db.get_articles_by_ids = lambda ids: [{
        "policy_crawler_article_id": "a1", "source_code": "qifuyun", "source_item_id": "q1",
        "kms_document_id": "d" * 32, "base_id": "base", "title": "已存在标题", "kms_payload_json": json.dumps(saved)}]
    class BoomKms:
        def push(self, payload): raise AssertionError("知识库已存在同标题时不应调用 KMS")
        def close(self): pass
    monkeypatch.setattr(engine_mod, "KmsClient", lambda settings: BoomKms())
    monkeypatch.setattr(engine_mod.kms_kb, "title_exists_in_base", lambda settings, title, base_id: True)
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    manager._stop_flags["run"] = threading.Event(); manager._active_run = "run"
    manager._execute_push_ids("run", ["a1"], False)
    assert db.article_results[0][0] == "a1"
    assert db.article_results[0][1].success and db.article_results[0][1].code == "7"
    assert manager._active_run is None


def test_push_articles_request_validation():
    assert PushArticlesRequest(article_ids=["a"], confirm_write=True).article_ids == ["a"]
    with pytest.raises(ValidationError):
        PushArticlesRequest(article_ids=[])


def test_crawl_skips_duplicate_title_within_batch():
    db = FakeDb()
    db.find_existing_by_title = lambda title: False
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    candidate = PolicyCandidate(source_code="qifuyun", source_item_id="q1", project_name="项目", detail_ref="q1")
    class FakeAdapter:
        def fetch(self, _):
            return PolicyArticle(source_code="qifuyun", source_item_id="q1", source_name="源", title="相同标题", project_name="项目", original_url="https://example.com", raw_content_html="<p>正文</p>")
    seen = set()
    first = manager._process_candidate("run", "qifuyun_declare", TASKS["qifuyun_declare"], candidate, None, False, FakeAdapter(), None, push_kms=False, seen_titles=seen)
    second = manager._process_candidate("run", "qifuyun_declare", TASKS["qifuyun_declare"], candidate, None, False, FakeAdapter(), None, push_kms=False, seen_titles=seen)
    assert first == "succeeded" and second == "skipped"


def test_crawl_skips_title_already_in_db():
    db = FakeDb()
    db.find_existing_by_title = lambda title: True
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    candidate = PolicyCandidate(source_code="qifuyun", source_item_id="q1", project_name="项目", detail_ref="q1")
    class FakeAdapter:
        def fetch(self, _):
            return PolicyArticle(source_code="qifuyun", source_item_id="q1", source_name="源", title="已存在标题", project_name="项目", original_url="https://example.com", raw_content_html="<p>正文</p>")
    outcome = manager._process_candidate("run", "qifuyun_declare", TASKS["qifuyun_declare"], candidate, None, False, FakeAdapter(), None, push_kms=False, seen_titles=set())
    assert outcome == "skipped"


def test_event_store_replays_after_last_event_id():
    store=EventStore(); store.emit("r","log","one"); store.emit("r","log","two")
    assert [e["message"] for e in store.wait_after("r",1,0)]==["two"]
