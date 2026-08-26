# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import DISCLAIMER, inject_css
from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.models import (
    EvidenceItem,
    ReviewDecision,
    RevisionChanges,
    ThesisAssessment,
    ThesisLifecycleStatus,
)
from thesis.repository import NotFoundError, SQLiteThesisRepository


MODE_LABELS = {
    "recorded": "Recorded/Fake · 离线示例，非真实研究",
}
LIFECYCLE_LABELS = {
    ThesisLifecycleStatus.DRAFT: "待确认",
    ThesisLifecycleStatus.ACTIVE: "研究中",
    ThesisLifecycleStatus.INVALIDATED: "已失效",
    ThesisLifecycleStatus.CLOSED: "已关闭",
    ThesisLifecycleStatus.REJECTED: "已拒绝",
}
ASSESSMENT_LABELS = {
    ThesisAssessment.PENDING: "待判断",
    ThesisAssessment.STRENGTHENING: "增强",
    ThesisAssessment.UNCHANGED: "不变",
    ThesisAssessment.WEAKENING: "减弱",
    ThesisAssessment.CONFLICTED: "冲突",
}


def _mode_label(generator_kind: str) -> str:
    if generator_kind.startswith("openai:"):
        return f"Live Model · {generator_kind.removeprefix('openai:')}"
    if generator_kind.startswith("claude-mcp:"):
        return (
            "Claude Desktop + MCP · 交互式研究 · "
            f"{generator_kind.removeprefix('claude-mcp:')}"
        )
    return MODE_LABELS.get(generator_kind, f"Recorded/Fake · {generator_kind} · 非真实研究")


def _proposal_review_for_revision(repository, revision):
    proposal = revision
    if revision.revision_type.value == "user_revision" and revision.derived_from_revision_id:
        proposal = repository.get_revision(revision.derived_from_revision_id)
    try:
        return repository.get_proposal_review(proposal.revision_id)
    except NotFoundError:
        return None


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _render_evidence(title: str, rows: list[EvidenceItem]) -> None:
    st.markdown(f"**{title}**")
    if not rows:
        st.caption("当前没有该方向的证据。")
        return
    for item in rows:
        st.write(f"- {item.claim}")
        st.caption(
            f"类型 {item.claim_type.value} · 质量 {item.evidence_quality.value} · "
            f"观测时间 {item.observed_at.isoformat()}"
        )
        with st.expander(f"来源与限制 · {str(item.evidence_id)[:8]}"):
            for source in item.source_refs:
                st.write(
                    f"{source.provider} · 数据截至 {source.data_as_of or '未知'} · "
                    f"取回 {source.retrieved_at.isoformat()}"
                )
                for limitation in source.known_limitations:
                    st.caption(f"限制：{limitation}")
            st.caption("Observation：" + "、".join(item.observation_ref_ids or ["无"]))
            for limitation in item.known_limitations:
                st.caption(f"证据限制：{limitation}")


def _render_list(title: str, values: list[str], empty: str) -> None:
    st.markdown(f"**{title}**")
    if values:
        for value in values:
            st.write(f"- {value}")
    else:
        st.caption(empty)


def _review_actions(repository: SQLiteThesisRepository, proposal) -> None:
    comment = st.text_input(
        "决策备注（可选）",
        key=f"comment-{proposal.revision_id}",
    )
    accept, reject = st.columns(2)
    if accept.button("接受研究", key=f"accept-{proposal.revision_id}", type="primary"):
        repository.review_proposal(
            proposal.revision_id,
            ReviewDecision.ACCEPT,
            user_comment=comment or None,
        )
        st.success("已接受 proposal；accepted revision 与生命周期已持久化。")
        st.rerun()
    if reject.button("拒绝研究", key=f"reject-{proposal.revision_id}"):
        repository.review_proposal(
            proposal.revision_id,
            ReviewDecision.REJECT,
            user_comment=comment or None,
        )
        st.success("已拒绝 proposal；候选与证据历史仍保留。")
        st.rerun()

    with st.expander("修改后接受"):
        with st.form(f"modify-{proposal.revision_id}"):
            expectation = st.text_area(
                "市场正在交易的预期",
                value=proposal.market_expectation,
            )
            assessment_values = list(ThesisAssessment)
            assessment = st.selectbox(
                "判断状态",
                assessment_values,
                index=assessment_values.index(proposal.assessment),
                format_func=lambda value: ASSESSMENT_LABELS[value],
            )
            price_in = st.text_area(
                "Price In 风险（每行一项）",
                value="\n".join(proposal.price_in_risks),
            )
            invalidation = st.text_area(
                "失效条件（每行一项）",
                value="\n".join(proposal.invalidation_conditions),
            )
            response = st.text_area(
                "失效后的处理",
                value=proposal.invalidation_response or "",
            )
            lifecycle_options = [None, *list(ThesisLifecycleStatus)]
            lifecycle = st.selectbox(
                "接受后生命周期",
                lifecycle_options,
                index=(
                    lifecycle_options.index(proposal.proposed_lifecycle_status)
                    if proposal.proposed_lifecycle_status in lifecycle_options
                    else 0
                ),
                format_func=lambda value: "按现有状态规则" if value is None else LIFECYCLE_LABELS[value],
            )
            modify_comment = st.text_input("修改说明（可选）")
            submitted = st.form_submit_button("保存修改并接受")
            if submitted:
                repository.review_proposal(
                    proposal.revision_id,
                    ReviewDecision.MODIFY,
                    changes=RevisionChanges(
                        market_expectation=expectation.strip() or proposal.market_expectation,
                        assessment=assessment,
                        price_in_risks=_lines(price_in),
                        invalidation_conditions=_lines(invalidation),
                        invalidation_response=response.strip() or None,
                        proposed_lifecycle_status=lifecycle,
                    ),
                    user_comment=modify_comment or None,
                )
                st.success("用户修订已保存并成为 accepted revision。")
                st.rerun()


inject_css()
st.title("ThesisCard 深度研究工作台")
st.caption("Agent 只提交 proposal；正式判断只有在你接受或修改后才会更新。")
st.caption(
    "Claude Desktop + MCP 是需要你主动发起对话的手动研究入口，不会由 PROMOTE 自动触发；"
    "它与 OpenAI 自动化路径不是同一种运行方式。"
)
st.page_link("pages/6_candidates.py", label="← 返回候选箱")

database = Path(os.environ.get("THESIS_DB_PATH", str(ROOT / "data" / "thesis.db")))
with SQLiteCandidateRepository(database) as candidate_repository:
    candidate_names = {
        candidate.instrument_id: candidate.instrument_name
        for candidate in candidate_repository.list()
    }
with SQLiteThesisRepository(database) as repository:
    cards = repository.list_cards()
    if not cards:
        st.info("当前没有 ThesisCard。请先在候选箱点击“转入研究”。")
    for card in cards:
        pending = repository.list_pending_proposals(card.thesis_id)
        accepted = repository.get_current_accepted_revision(card.thesis_id)
        with st.container(border=True):
            st.subheader(
                f"{card.instrument.name or candidate_names.get(card.instrument.instrument_id) or card.instrument.code} · "
                f"{card.instrument.code}.{card.instrument.exchange.value}"
            )
            st.caption(
                f"生命周期：{LIFECYCLE_LABELS[card.lifecycle_status]} · "
                f"发现来源：{card.discovery_source.value} · "
                f"Thesis：{card.thesis_id}"
            )
            if accepted is not None:
                accepted_review = _proposal_review_for_revision(repository, accepted)
                if accepted_review is not None:
                    accepted_mode = _mode_label(accepted_review.generator_kind)
                    if accepted_review.generator_kind == "recorded":
                        st.warning(accepted_mode)
                    else:
                        st.success(accepted_mode)
                st.success(
                    f"accepted revision：v{accepted.version} · "
                    f"{ASSESSMENT_LABELS[accepted.assessment]}"
                )
                st.write(accepted.market_expectation)
            elif not pending:
                st.info("该 ThesisCard 当前没有待审核或已接受的 revision。")

            for proposal in pending:
                review = repository.get_proposal_review(proposal.revision_id)
                mode_label = _mode_label(review.generator_kind)
                if review.generator_kind == "recorded":
                    st.warning(mode_label)
                else:
                    st.success(mode_label)
                st.markdown(f"### 待审核 proposal · v{proposal.version}")
                st.write(proposal.market_expectation)
                _render_evidence("支持证据", proposal.support_evidence)
                _render_evidence("反对证据", proposal.counter_evidence)
                _render_list("Price In 风险", proposal.price_in_risks, "当前 proposal 未生成 Price In 风险。")
                _render_list("失效条件", proposal.invalidation_conditions, "当前没有结构化失效条件。")
                if proposal.invalidation_response:
                    st.caption(f"失效处理：{proposal.invalidation_response}")

                st.markdown("**Semantic Reviewer issues**")
                st.write(review.semantic_review.summary)
                if review.semantic_review.issues:
                    for issue in review.semantic_review.issues:
                        st.warning(f"{issue.issue_code}：{issue.message}")
                else:
                    st.info("Reviewer 未发现额外问题；仍需用户判断。")
                with st.expander("研究链路审计"):
                    st.write(" → ".join(review.graph_trace))
                    st.caption(f"Generator：{review.generator_kind} · Repair：{review.repair_count}")
                _review_actions(repository, proposal)

            decisions = repository.list_decisions(card.thesis_id)
            if decisions:
                with st.expander("Human-in-the-loop 决策历史"):
                    for event in decisions:
                        st.write(
                            f"{event.created_at.isoformat()} · {event.decision.value} · "
                            f"proposal {event.proposal_revision_id}"
                        )
                        if event.user_comment:
                            st.caption(event.user_comment)

st.markdown(DISCLAIMER, unsafe_allow_html=True)
