"""Traceability of the control scenarios SC-01…SC-09 to the tests that check them (SECURITY_MODEL §18.2).

Each scenario lists the clauses of its expected behaviour (SECURITY_MODEL §18.1) and the tests of each clause.
``scripts/tests/test_security_scenarios.py`` (CI job "Repository") checks that every referenced test
exists, so the table cannot point at a renamed or deleted test. Backend references are pytest node ids
relative to ``apps/backend``; frontend and E2E references are ``path::title`` relative to the repository root.

Run the backend tests of every scenario (in the backend container)::

    pytest $(python -m tests.security_scenarios)
"""

from __future__ import annotations

import sys

UNIT = "tests/unit/"
INT = "tests/integration/"
CONTRACT = "tests/contract/"
FRONTEND = "apps/frontend/src/"
E2E = "apps/e2e/tests/"

SCENARIOS: dict[str, dict[str, list[str]]] = {
    "SC-01": {
        "the assistant is not listed, its card is 404": [
            INT + "test_assistant_access.py::test_sc01_access_to_one_of_two_knowledge_bases_is_not_enough",
            E2E + "access.spec.ts::shows only the knowledge bases and assistants the member may use",
        ],
        "the chat refuses with 404, with 403 assistant_unavailable in one's own conversation": [
            INT + "test_chat_stream.py::test_sc01_an_assistant_on_two_knowledge_bases_needs_read_on_both",
            INT + "test_chat_stream.py::test_an_assistant_the_caller_cannot_use",
            E2E + "access.spec.ts::refuses a hand-made request to the Finance assistant",
        ],
        "no chunk of either knowledge base reaches the model": [
            INT + "test_chat_stream.py::test_sc01_an_assistant_on_two_knowledge_bases_needs_read_on_both",
            INT + "test_chat_stream.py::test_only_the_assistants_knowledge_bases_reach_the_model",
            E2E + "access.spec.ts::never sends a Finance document to the model",
        ],
    },
    "SC-02": {
        "no API changes Instance Settings or the allowlist": [
            UNIT + "test_route_inventory.py::test_sc02_no_route_changes_instance_settings",
            INT + "test_groups_api.py::test_instance_settings_are_read_only_and_secret_free",
        ],
        "a provider with a base_url outside the policy is 422 endpoint_not_allowed": [
            INT + "test_providers_api.py::test_endpoint_outside_the_network_policy_is_refused",
        ],
    },
    "SC-03": {
        "after initialization 409 setup_already_completed with any token, recorded": [
            INT + "test_setup_api.py::test_repeated_initialization_is_rejected_with_any_token",
            INT + "test_setup_api.py::test_reissuing_token_after_initialization_does_not_reopen_setup",
        ],
        "before initialization 403 setup_token_invalid without a valid token, recorded": [
            INT + "test_setup_api.py::test_wrong_token_is_rejected",
            INT + "test_setup_api.py::test_expired_token_is_rejected",
            INT + "test_setup_api.py::test_rotated_token_invalidates_previous_one",
            INT + "test_setup_api.py::test_no_token_issued_means_setup_is_closed",
        ],
    },
    "SC-04": {
        "two parallel initializations: one 201, the other 409, one administrator": [
            INT + "test_setup_api.py::test_parallel_initialization_creates_exactly_one_admin",
            INT + "test_setup_api.py::test_failure_mid_transaction_leaves_instance_uninitialized",
        ],
    },
    "SC-05": {
        "card, list, versions, reindex and delete of a document without access are 404": [
            INT + "test_documents_api.py::test_documents_are_hidden_without_access_sc05",
            INT + "test_rbac_matrix.py::test_knowledge_base_routes_follow_the_access_level",
            INT + "test_rbac_matrix.py::test_deleting_a_document_needs_manage",
        ],
        "the viewer and the chunks are 404 without access": [
            INT + "test_source_viewer.py::test_without_access_the_chunk_does_not_exist",
            INT + "test_source_viewer.py::test_a_chunk_is_served_only_for_its_own_document_and_version",
            E2E + "access.spec.ts::does not open a Finance passage in the source viewer",
        ],
        "search without access is 404 and calls no provider": [
            INT + "test_retrieval.py::test_user_without_access_gets_not_found_and_no_provider_call",
        ],
    },
    "SC-06": {
        "an allowlisted local endpoint is reachable for its purpose and provider type": [
            INT + "test_network_egress.py::test_allowlisted_endpoint_is_reachable_and_shown_in_settings",
        ],
        "the same address for another purpose or provider type is refused": [
            UNIT
            + "test_safe_http_client.py::test_same_address_for_another_purpose_or_provider_type_is_denied",
            CONTRACT
            + "test_ai_providers.py::test_connection_test_of_an_endpoint_outside_policy_is_egress_denied",
            INT + "test_providers_api.py::test_endpoint_outside_the_network_policy_is_refused",
        ],
    },
    "SC-07": {
        "an allowed name resolving to a forbidden address is never connected": [
            UNIT + "test_safe_http_client.py::test_allowlisted_name_resolving_elsewhere_is_denied",
        ],
        "the checked address is the one connected to (no second resolution)": [
            UNIT + "test_safe_http_client.py::test_dns_rebinding_cannot_swap_the_address_after_the_check",
        ],
        "the denial is network.egress.denied in the audit log": [
            INT + "test_network_egress.py::test_denied_connection_is_audited_without_path_or_query",
        ],
    },
    "SC-08": {
        "document text stays data, apart from the rules": [
            UNIT
            + "test_retrieval_context.py::test_rules_stay_in_the_instructions_and_documents_in_the_context",
            UNIT
            + "test_retrieval_context.py::test_a_document_cannot_close_the_block_or_forge_another_document",
            UNIT
            + "test_retrieval_context.py::test_rules_forbid_obeying_documents_links_images_and_prompt_disclosure",  # noqa: E501
            INT + "test_source_sync.py::test_sc08_a_synced_document_reaches_the_prompt_only_as_escaped_data",
        ],
        "the model gets no tools": [
            CONTRACT + "test_ai_providers.py::test_openai_chat_streams_deltas_then_finish_with_usage",
            CONTRACT
            + "test_ai_providers.py::test_anthropic_chat_uses_messages_api_without_sampling_parameters",
        ],
        "invented citation ids are removed": [
            UNIT + "test_retrieval_context.py::test_valid_ids_pass_and_unknown_ones_are_removed",
            UNIT + "test_retrieval_context.py::test_an_id_from_an_earlier_generation_is_rejected",
            INT + "test_chat_stream.py::test_answer_streams_with_validated_citations",
        ],
        "external images in an answer are not loaded": [
            FRONTEND
            + "features/chat/AnswerMarkdown.test.tsx::does not load images: they become a link to their address",  # noqa: E501
            FRONTEND + "features/chat/AnswerMarkdown.test.tsx::never renders raw HTML from the answer",
        ],
        "no secret is in the prompt": [
            UNIT + "test_retrieval_context.py::test_no_secret_reaches_the_prompt",
        ],
    },
    "SC-09": {
        "a repeated job is skipped_already_processed and changes no rows": [
            INT + "test_indexing.py::test_repeated_job_changes_nothing_sc09",
        ],
        "two concurrent jobs create no duplicates": [
            INT + "test_indexing.py::test_concurrent_runs_do_not_duplicate_rows",
        ],
    },
}


def backend_node_ids() -> list[str]:
    ids = {
        reference
        for clauses in SCENARIOS.values()
        for references in clauses.values()
        for reference in references
        if reference.startswith("tests/")
    }
    return sorted(ids)


if __name__ == "__main__":
    sys.stdout.write("\n".join(backend_node_ids()) + "\n")
