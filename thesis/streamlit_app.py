from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from thesis.models import ReviewDecision
from thesis.repository import SQLiteThesisRepository


def render_workbench(database: str | Path) -> None:
    st.set_page_config(page_title="短线预期工作台", layout="wide")
    st.title("短线预期工作台")
    st.caption("Agent 只提交 proposal；正式判断仅在用户明确决策后更新。")

    repository = SQLiteThesisRepository(database)
    try:
        pending = repository.list_pending_proposals()
        if not pending:
            st.info("当前没有待审核 proposal。")
            return
        for proposal in pending:
            card = repository.get_card(proposal.thesis_id)
            review = repository.get_proposal_review(proposal.revision_id)
            with st.container(border=True):
                st.subheader(f"{card.instrument.name or card.instrument.code} · v{proposal.version}")
                st.write(proposal.market_expectation)
                st.write(f"当前生命周期：{card.lifecycle_status.value}")
                st.write(f"当前 accepted revision：{card.current_accepted_revision_id or '无'}")
                st.markdown("**Semantic Reviewer**")
                st.write(review.semantic_review.summary)
                if review.semantic_review.issues:
                    for issue in review.semantic_review.issues:
                        st.warning(f"{issue.issue_code}: {issue.message}")
                else:
                    st.info("Reviewer 未发现额外语义提示；仍需用户判断。")
                if st.button("Accept", key=f"accept-{proposal.revision_id}"):
                    repository.review_proposal(proposal.revision_id, ReviewDecision.ACCEPT)
                    st.success("已接受 proposal；accepted revision 已持久化。")
                    st.rerun()
    finally:
        repository.close()


def main() -> None:
    default_database = Path(__file__).resolve().parents[1] / "data" / "thesis.db"
    render_workbench(os.environ.get("THESIS_DB_PATH", str(default_database)))


if __name__ == "__main__":
    main()
