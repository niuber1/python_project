import json
import threading

import pytest
from pydantic import ValidationError

from crawler_tool.config import TASKS, Settings
from crawler_tool.engine import RunManager
from crawler_tool.events import EventStore
from crawler_tool.models import KmsResult, PolicyArticle, PolicyCandidate, PushArticlesRequest, StartRunRequest


class FakeDb:
    def __init__(self): self.items=[]; self.article_results=[]; self.run_updates=[]; self.claimed=[]; self.released=[]
    def create_run_item(self, **values): self.items.append(values)
    def update_run_item(self, item_id, **values): self.items.append({"id":item_id, **values})
    def update_article_kms(self, article_id, result): self.article_results.append((article_id,result))
    def insert_article(self, *args, **kwargs): return True
    def update_run(self, run_id, **values): self.run_updates.append(values)
    def create_run(self, run_id, trigger_type, task_codes, dry_run): self.run_updates.append({"id": run_id, "trigger": trigger_type, "codes": task_codes, "dry_run": dry_run})
    def refresh_existing_article(self, *args): self.refreshed = args
    def find_existing(self, *args): return {}
    def find_existing_by_title(self, *args): return False
    def existing_source_ids(self, *args): return set()
    def article_titles_in_bases(self, *args): return set()
    def create_skipped_run_items(self, rows): self.items.extend(rows)
    def claim_articles_for_push(self, article_ids):
        self.claimed.append(list(article_ids))
        return {article_id: "pending" for article_id in article_ids}
    def release_articles_from_processing(self, statuses): self.released.append(dict(statuses))


class NeverFetch:
    def fetch(self, _): raise AssertionError("已存在失败记录不应重新抓网页")


class FakeKms:
    def push(self, payload): return KmsResult(success=True, code="1", message="ok")
    def close(self): pass


def test_payload_includes_policy_header_fields_in_kms_metadata():
    from datetime import date

    article = PolicyArticle(
        source_code="suishenban", source_item_id="shanghai-gwk:test", source_name="上海一网通办",
        title="政策标题", project_name="政策标题", original_url="https://example.com/policy",
        publish_date=date(2023, 5, 19), document_no="奉人社〔2023〕9号", publish_dept="奉人社",
        raw_content_html="<p>纯正文</p>",
    )
    payload = RunManager._payload(article, "<p>纯正文</p>", "base")
    assert payload.publish_date == "2023-05-19" and payload.document_no == "奉人社〔2023〕9号"
    assert payload.metadata["标题"] == "政策标题"
    assert payload.metadata["印发日期"] == "2023-05-19"
    assert payload.metadata["发布日期"] == "2023-05-19"
    assert payload.metadata["发文日期"] == "2023-05-19"
    assert payload.metadata["文号"] == "奉人社〔2023〕9号"
    assert payload.metadata["发文文号"] == "奉人社〔2023〕9号"


def test_non_declare_task_uses_its_own_base_and_suishenban_filter():
    import httpx

    db = FakeDb()
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    adapter = manager._adapter("suishenban_non_declare", httpx.Client())
    assert adapter.free_enjoy is True
    article = PolicyArticle(
        source_code="suishenban", source_item_id="policy-1", source_name="上海一网通办",
        title="非申报政策", project_name="非申报政策", original_url="https://example.com/policy",
        raw_content_html="<p>正文</p>",
    )
    payload = manager._payload(article, "<p>正文</p>", TASKS["suishenban_non_declare"]["base_id"])
    assert payload.base_id == "c24a793eec08458f873d263a090361d0"


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
    assert candidate.project_name == "标题"


def test_crawl_phase_skips_existing_article():
    db=FakeDb(); manager=RunManager(Settings(_env_file=None),db,EventStore())
    existing={"policy_crawler_article_id":"article","kms_status":"failed","kms_result_code":"4"}
    candidate=PolicyCandidate(source_code="qifuyun",source_item_id="q1",project_name="项目",detail_ref="q1")
    outcome=manager._process_candidate("run","qifuyun_declare",TASKS["qifuyun_declare"],candidate,existing,False,NeverFetch(),FakeKms(),push_kms=False)
    assert outcome=="skipped"


def test_refresh_existing_only_marks_changed_document_for_manual_update():
    db=FakeDb(); manager=RunManager(Settings(_env_file=None, enable_content_update=True),db,EventStore())
    candidate=PolicyCandidate(source_code="qifuyun",source_item_id="q1",project_name="项目",detail_ref="q1")
    existing={"policy_crawler_article_id":"article","kms_status":"success","content_hash":"old"}
    class Adapter:
        def fetch(self,_):
            return PolicyArticle(source_code="qifuyun",source_item_id="q1",source_name="源",title="标题",project_name="项目",original_url="https://example.com",raw_content_html="<p>最新正文</p>")
    outcome=manager._process_candidate("run","qifuyun_declare",TASKS["qifuyun_declare"],candidate,existing,False,Adapter(),None,push_kms=False,refresh_existing=True)
    assert outcome=="succeeded" and db.refreshed[-1] is True


def test_refresh_existing_skips_when_content_hash_unchanged():
    from crawler_tool.html_utils import compose_document_content, content_sha256
    db=FakeDb(); manager=RunManager(Settings(_env_file=None, enable_content_update=True),db,EventStore())
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


def test_schedule_config_request_validates_daily_time():
    from crawler_tool.models import ScheduleConfigRequest

    value = ScheduleConfigRequest(enabled=True, hour=1, minute=30, task_codes=["suishenban_non_declare"])
    assert value.hour == 1 and value.task_codes == ["suishenban_non_declare"]
    with pytest.raises(ValidationError):
        ScheduleConfigRequest(hour=24, minute=0)


def test_content_update_disabled_rejects_refresh_and_manual_update():
    manager = RunManager(Settings(_env_file=None, enable_content_update=False), FakeDb(), EventStore())
    with pytest.raises(ValueError, match="正文更新功能当前已关闭"):
        manager.start(["qifuyun_declare"], False, refresh_existing=True)
    with pytest.raises(ValueError, match="正文更新功能当前已关闭"):
        manager.update_articles(["article"], False)


def test_url_run_only_stores_locally_and_does_not_construct_kms(monkeypatch):
    import crawler_tool.engine as engine_mod
    db = FakeDb()
    events = EventStore()
    manager = RunManager(Settings(_env_file=None), db, events)

    class Adapter:
        def fetch(self, candidate):
            return PolicyArticle(
                source_code="qifuyun", source_item_id=candidate.source_item_id, source_name="源", title="URL 政策",
                project_name="URL 政策", original_url="https://example.com/policy", raw_content_html="<p>正文</p>",
            )

    monkeypatch.setattr(manager, "_adapter", lambda task_code, client: Adapter())
    monkeypatch.setattr(engine_mod, "KmsClient", lambda _: (_ for _ in ()).throw(AssertionError("URL 抓取不得调用 KMS")))
    manager._stop_flags["url-run"] = threading.Event()
    manager._active_run = "url-run"
    manager._execute_url("url-run", ["https://shpolicy.ssme.sh.gov.cn/knowledge/#/policy?policyId=q-1"], False)
    assert db.article_results == []
    assert any(item.get("phase") == "store" and item.get("status") == "success" for item in db.items)
    assert any("政策标题：URL 政策\n原始 URL：https://shpolicy.ssme.sh.gov.cn/knowledge/#/policy?policyId=q-1" == event["message"] for event in events.wait_after("url-run", 0, 0))
    assert manager._active_run is None


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


def test_push_articles_claims_rows_before_background_thread_starts(monkeypatch):
    import crawler_tool.engine as engine_mod

    db = FakeDb()
    started = []

    class DeferredThread:
        def __init__(self, *, target, args, **_):
            self.target = target
            self.args = args

        def start(self):
            started.append(self.args)

    monkeypatch.setattr(engine_mod.threading, "Thread", DeferredThread)
    manager = RunManager(Settings(_env_file=None), db, EventStore())

    run_id = manager.push_articles(["a1", "a2", "a1"], False)

    assert db.claimed == [["a1", "a2"]]
    assert started and started[0][0] == run_id
    assert started[0][3] == {"a1": "pending", "a2": "pending"}


def test_push_articles_preview_does_not_claim_rows(monkeypatch):
    import crawler_tool.engine as engine_mod

    db = FakeDb()
    monkeypatch.setattr(engine_mod.threading, "Thread", type("DeferredThread", (), {
        "__init__": lambda self, **kwargs: setattr(self, "args", kwargs["args"]),
        "start": lambda self: None,
    }))
    manager = RunManager(Settings(_env_file=None), db, EventStore())

    manager.push_articles(["a1"], True)

    assert db.claimed == []


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
    db.find_existing_by_title = lambda title, base_id: False
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
    db.find_existing_by_title = lambda title, base_id: True
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    candidate = PolicyCandidate(source_code="qifuyun", source_item_id="q1", project_name="项目", detail_ref="q1")
    class FakeAdapter:
        def fetch(self, _):
            return PolicyArticle(source_code="qifuyun", source_item_id="q1", source_name="源", title="已存在标题", project_name="项目", original_url="https://example.com", raw_content_html="<p>正文</p>")
    outcome = manager._process_candidate("run", "qifuyun_declare", TASKS["qifuyun_declare"], candidate, None, False, FakeAdapter(), None, push_kms=False, seen_titles=set())
    assert outcome == "skipped"


def test_crawl_skips_title_preloaded_from_kms_without_inserting_locally():
    db = FakeDb()
    db.find_existing_by_title = lambda title, base_id: False
    db.insert_article = lambda *args, **kwargs: pytest.fail("KMS 已有标题不应写入本地台账")
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    candidate = PolicyCandidate(source_code="suishenban", source_item_id="s1", project_name="项目", detail_ref="s1")

    class FakeAdapter:
        def fetch(self, _):
            return PolicyArticle(
                source_code="suishenban", source_item_id="s1", source_name="源", title="KMS 已有政策",
                project_name="项目", original_url="https://example.com", raw_content_html="<p>正文</p>",
            )

    outcome = manager._process_candidate(
        "run", "suishenban_non_declare", TASKS["suishenban_non_declare"], candidate, None,
        False, FakeAdapter(), None, push_kms=False, seen_titles=set(), kms_titles={"KMS 已有政策"},
    )
    assert outcome == "skipped"
    assert any(item.get("message") == "KMS 知识库已存在同标题，跳过" for item in db.items)


def test_platform_classification_routes_base_and_uses_matching_kms_title_set(monkeypatch):
    import crawler_tool.engine as engine_mod
    from crawler_tool.config import NON_DECLARE_BASE_ID

    db = FakeDb()
    db.find_existing_any_base = lambda *_: None
    db.find_existing_by_title = lambda *_: False
    db.insert_article = lambda *args, **kwargs: pytest.fail("对应知识库已有标题时不应写本地台账")
    class Classifier:
        def __init__(self, *_): pass
        def classify(self, *_):
            return type("Result", (), {"base_id": NON_DECLARE_BASE_ID, "applicable_type": "惠企", "policy_type": "非申报通知类"})()
    monkeypatch.setattr(engine_mod, "PolicyClassifier", Classifier)
    candidate = PolicyCandidate(source_code="shanghai_policy_platform", source_item_id="0001:b1", project_name="平台政策", detail_ref="b1")
    class Adapter:
        client = object()
        def fetch(self, _):
            return PolicyArticle(source_code="shanghai_policy_platform", source_item_id="0001:b1", source_name="上海市统一政策发布平台", title="已存在的非申报标题", project_name="平台政策", original_url="https://example.com", raw_content_html="<p>正文</p>")
    events = EventStore()
    outcome = RunManager(Settings(_env_file=None), db, events)._process_candidate(
        "run", "shanghai_policy_platform", TASKS["shanghai_policy_platform"], candidate, None,
        False, Adapter(), None, push_kms=False, seen_titles=set(),
        kms_titles_by_base={NON_DECLARE_BASE_ID: {"已存在的非申报标题"}},
    )
    assert outcome == "skipped"
    assert any(item.get("message") == "KMS 知识库已存在同标题，跳过" for item in db.items)
    classification_messages = [
        event["message"] for event in events.wait_after("run", 0, 0)
        if event["message"].startswith("智能体判断：")
    ]
    assert classification_messages == ["智能体判断：applicable_type=惠企；policy_type=非申报通知类"]
    assert all("政策标题：" not in message for message in classification_messages)


def test_platform_prefilter_skips_before_detail_and_preserves_reason_priority():
    from crawler_tool.config import NON_DECLARE_BASE_ID, TARGET_BASE_ID

    db = FakeDb()
    db.existing_source_ids = lambda source_code: {"0001:source-existing"}
    db.article_titles_in_bases = lambda base_ids: {"本地标题"}
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    candidates = [
        PolicyCandidate(source_code="shanghai_policy_platform", source_item_id="0001:source-existing", project_name="来源已存在", detail_ref="1"),
        PolicyCandidate(source_code="shanghai_policy_platform", source_item_id="0001:local-title", project_name="本地标题", detail_ref="2"),
        PolicyCandidate(source_code="shanghai_policy_platform", source_item_id="0001:kms-title", project_name="KMS 标题", detail_ref="3"),
        PolicyCandidate(source_code="shanghai_policy_platform", source_item_id="0001:new-1", project_name="新标题", detail_ref="4"),
        PolicyCandidate(source_code="shanghai_policy_platform", source_item_id="0001:new-2", project_name="新标题", detail_ref="5"),
    ]

    fresh, skipped, summary = manager._prefilter_platform_candidates(
        candidates,
        TASKS["shanghai_policy_platform"],
        {TARGET_BASE_ID: set(), NON_DECLARE_BASE_ID: {"KMS 标题"}},
    )

    assert [item.source_item_id for item in fresh] == ["0001:new-1"]
    assert [reason for _, reason in skipped] == [
        "来源 ID 已存在于本地台账，提前跳过",
        "本地台账已存在同标题，提前跳过",
        "KMS 任一目标知识库已存在同标题，提前跳过",
        "当前批次标题重复，提前跳过",
    ]
    assert summary == {"source_id": 1, "local_title": 1, "kms_title": 1, "batch_title": 1}


def test_platform_prefilter_does_not_use_classification_failure_table():
    db = FakeDb()
    manager = RunManager(Settings(_env_file=None), db, EventStore())
    candidate = PolicyCandidate(
        source_code="shanghai_policy_platform", source_item_id="0001:failed-before",
        project_name="曾经分类失败", detail_ref="1",
    )

    fresh, skipped, summary = manager._prefilter_platform_candidates(
        [candidate], TASKS["shanghai_policy_platform"], {},
    )

    assert fresh == [candidate] and skipped == []
    assert summary == {"source_id": 0, "local_title": 0, "kms_title": 0, "batch_title": 0}


def test_event_store_replays_after_last_event_id():
    store=EventStore(); store.emit("r","log","one"); store.emit("r","log","two")
    assert [e["message"] for e in store.wait_after("r",1,0)]==["two"]


def test_event_store_replays_persisted_history_after_restart():
    class EventRepository:
        rows = []

        def append_run_event(self, run_id, event_type, message, data, created_at):
            event_id = len(self.rows) + 1
            self.rows.append({"id": event_id, "type": event_type, "message": message, "time": created_at.isoformat(timespec="seconds"), **data})
            return event_id

        def recent_run_events(self, run_id, limit):
            return [row for row in self.rows if row.get("run_id", run_id) == run_id][-limit:]

        def list_run_events(self, run_id, after_id, limit=200):
            return [row for row in self.rows if row.get("run_id", run_id) == run_id and row["id"] > after_id][:limit]

    repository = EventRepository()
    EventStore(repository).emit("run", "status", "任务开始", total=1)
    restarted_store = EventStore(repository)

    assert [event["message"] for event in restarted_store.recent("run")] == ["任务开始"]
    assert [event["message"] for event in restarted_store.wait_after("run", 0, 0)] == ["任务开始"]
