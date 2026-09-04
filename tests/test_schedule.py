from apscheduler.schedulers.background import BackgroundScheduler
import pytest

import crawler_tool.app as app_mod
from crawler_tool.models import ScheduleConfigRequest


class FakeScheduleDb:
    def __init__(self):
        self.config = None

    def get_schedule_config(self):
        return self.config

    def save_schedule_config(self, enabled, hour, minute, task_codes):
        self.config = {
            "enabled": enabled,
            "hour": hour,
            "minute": minute,
            "task_codes": list(task_codes),
            "updated_at": None,
        }
        return self.config


def test_schedule_update_replaces_job_with_selected_tasks(monkeypatch):
    fake_db = FakeScheduleDb()
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.start()
    monkeypatch.setattr(app_mod, "database", fake_db)
    monkeypatch.setattr(app_mod, "scheduler", scheduler)
    try:
        result = app_mod.set_schedule(ScheduleConfigRequest(
            enabled=True, hour=2, minute=15, task_codes=["suishenban_non_declare"],
        ))
        job = scheduler.get_job("daily-policy-crawler")
        assert result["enabled"] is True
        assert job is not None and job.args == (['suishenban_non_declare'],)
        assert "hour='2'" in str(job.trigger) and "minute='15'" in str(job.trigger)
    finally:
        scheduler.shutdown(wait=False)


def test_enabled_schedule_requires_a_task():
    with pytest.raises(app_mod.HTTPException, match="至少选择一个任务"):
        app_mod.set_schedule(ScheduleConfigRequest(enabled=True, hour=1, minute=0, task_codes=[]))
