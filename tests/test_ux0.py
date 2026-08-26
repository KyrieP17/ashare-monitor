from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from thesis.freshness import artifact_freshness


ROOT = Path(__file__).resolve().parents[1]
LEGACY_PAGES = (
    "1_ladder.py",
    "2_sector_flow.py",
    "3_us_market.py",
    "4_watchlist.py",
    "5_tracker.py",
)


def test_runtime_is_reproducibly_pinned_to_numpy_1x_project_environment():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "start_local_app.ps1").read_text(encoding="utf-8")
    runtime_check = (ROOT / "scripts" / "check_runtime.py").read_text(encoding="utf-8")

    assert "numpy==1.26.4" in requirements
    assert "pandas==2.2.2" in requirements
    assert "pyarrow==16.1.0" in requirements
    assert ".venv\\Scripts\\python.exe" in launcher
    assert "-m streamlit run app.py" in launcher
    for package in ("numpy", "pandas", "pyarrow", "streamlit", "plotly"):
        assert package in runtime_check
    assert "outside" in runtime_check.lower() or "external" in runtime_check.lower()


def test_all_legacy_pages_use_shared_freshness_and_no_duplicate_page_config():
    for filename in LEGACY_PAGES:
        source = (ROOT / "pages" / filename).read_text(encoding="utf-8")
        assert "render_legacy_freshness" in source
        assert "set_page_config" not in source


def test_stale_ladder_disables_old_risk_score_role_and_trajectory_language():
    common = (ROOT / "common.py").read_text(encoding="utf-8")
    ladder = (ROOT / "pages" / "1_ladder.py").read_text(encoding="utf-8")

    assert "当前展示的是历史数据，不代表今日市场状态。" in common
    assert "旧版风险结论已停用；以下仅为对应历史交易日记录。" in common
    assert "旧规则注意项（仅针对该交易日）" in common
    assert "stale=freshness.stale" in ladder


def test_latest_trade_day_metadata_drives_freshness():
    beijing = timezone(timedelta(hours=8))
    result = artifact_freshness(
        {"meta": {"latest_trade_day": "20260825", "generated_at": "2026-08-26T08:00:00"}},
        now=datetime(2026, 8, 26, 10, 0, tzinfo=beijing),
    )
    assert result.trade_date.isoformat() == "2026-08-25"
    assert result.stale is True


def test_candidate_ui_has_two_regions_localized_actions_and_missing_values():
    source = (ROOT / "pages" / "6_candidates.py").read_text(encoding="utf-8")

    assert '"我的关注"' in source
    assert '"市场发现"' in source
    assert '"保留关注"' in source
    assert '"忽略"' in source
    assert '"转入研究"' in source
    assert '"查看价格行为"' in source
    assert '"数据不足"' in source
    assert "removeprefix(prefix)" in source
    assert "st.columns(2" in source
