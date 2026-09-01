from __future__ import annotations

import streamlit as st

from .candidate_repository import SQLiteCandidateRepository
from .scan_health import derive_loop_health, derive_run_display, format_beijing


def render_scan_status(repository: SQLiteCandidateRepository, *, expanded: bool = True) -> None:
    latest = repository.latest_scan_run()
    latest_loop = repository.latest_loop_scan_run()
    st.markdown("#### 扫描状态")

    if latest is None:
        st.info("尚无扫描记录。页面存在不代表后台扫描器正在运行。")
    else:
        display = derive_run_display(latest)
        columns = st.columns(4)
        columns[0].metric("最近模式", latest.mode.value.upper())
        columns[1].metric("最近状态", display.label)
        columns[2].metric("Observation", latest.observation_count)
        columns[3].metric("本轮生成候选", latest.candidate_count)
        st.caption(
            f"开始（北京时间）{format_beijing(latest.started_at)} · "
            f"结束（北京时间）{format_beijing(latest.completed_at)} · "
            f"交易日 {latest.trade_date.isoformat() if latest.trade_date else '—'} · "
            f"ScanRun {latest.scan_run_id}"
        )
        with st.expander("数据源与错误详情", expanded=expanded and bool(latest.error_messages)):
            if latest.source_statuses:
                for item in latest.source_statuses:
                    st.markdown(
                        f"- `{item.source}` · {item.status.value} · "
                        f"Observation {item.observation_count} · 错误：{item.error_message or '—'}"
                    )
            if latest.error_messages:
                st.error("；".join(latest.error_messages))
            elif not latest.source_statuses:
                st.caption("本次记录没有数据源明细。")

    loop = derive_loop_health(latest_loop)
    getattr(st, loop.level)(f"持续 LOOP 健康状态：{loop.label}")
    if latest_loop is not None:
        st.caption(
            f"最近 LOOP：{format_beijing(latest_loop.started_at)} · "
            f"状态 {derive_run_display(latest_loop).label} · "
            f"间隔 {latest_loop.interval_seconds or '—'} 秒"
        )
        if latest_loop.expected_next_run_at is not None:
            st.caption(f"下一个预期扫描时间（北京时间）：{format_beijing(latest_loop.expected_next_run_at)}")
