"""Declared environment assistance, separate from model ability and hidden scoring."""


NOTICE_POLICIES = ("managed", "source_only")


def environment_support(notice_policy="managed", workspace_profile=None):
    if notice_policy not in NOTICE_POLICIES:
        raise ValueError("Unknown source-change notice policy")
    result = {
        "profile": "dependency_managed_dataops_v1",
        "notice_policy": notice_policy,
        "artifact_lineage": "paginated_object_detail",
        "inventory": "summary_search_detail_v1",
        "run_closure": "receipt_only_publication_handoff_required_other_cleanup_separate",
        "artifact_freshness": "recursive_source_revision_check_exposed_as_current",
        "source_change_notice": "explicit_invalidated_artifact_ids" if notice_policy == "managed" else "changed_source_and_revision_only",
        "normal_rereads": True,
        "automated_source_reconciliation": ["audit-region", "review-release", "publish-release"],
        "selection_acceptance": "interface_only_not_business_binding_validity",
        "error_feedback": "first_failed_public_business_condition_without_correct_answer_ids",
        "semantic_errors_can_propagate": True,
        "scope": "Skill execution with managed dependencies and automated audits; not transcript-only recovery",
    }
    if workspace_profile is not None:
        if workspace_profile != "release_maintenance_v2" or notice_policy != "source_only":
            raise ValueError("Unknown established-workspace support profile")
        result.update(profile=workspace_profile, artifact_lineage="paginated_object_detail",
                      inventory="summary_search_detail_history", initial_work="environment_origin_not_model_prefix",
                      public_progress="resolution_counts_and_consumer_link_presence_without_semantic_validation",
                      search="literal_text_and_typed_exact_filters",
                      amendment_validation="local_constraints_not_business_decision_oracle",
                      scope="Established release maintenance with persistent decisions and source-local optimistic concurrency")
    result["completion_observation"] = "deliverable_record_and_run_lifecycle_not_hidden_business_success"
    result["pagination"] = "zero_based_offset_limit_1_to_200"
    return result
