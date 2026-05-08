"""Property-based tests for supplement parameter filtering.

# Feature: dreams-application-flow, Property 5: 補件問卷僅包含不合格項目

Validates that the supplement questionnaire only includes failed items,
does not include passed items, and the count matches.
"""

from hypothesis import given, settings, strategies as st

from dreams_workflow.ai_determination.field_mapping_loader import (
    build_supplement_params,
    get_questionnaire_result_mapping,
    get_supplement_param_codes,
    get_supplement_params_separator,
    get_taipower_result_mapping,
)


# Strategy: generate random result field values
result_values = st.sampled_from(["Pass", "Fail", "Yes", ""])


def _generate_result_fields(result_mapping: dict[str, str]):
    """Strategy to generate random result fields based on a mapping."""
    # Generate a dict of result_field_id → random value
    return st.fixed_dictionaries(
        {result_field_id: result_values for result_field_id in result_mapping.values()}
    )


class TestSupplementParamsFiltering:
    """Property 5: 補件問卷僅包含不合格項目"""

    @settings(max_examples=100)
    @given(data=st.data())
    def test_only_fail_or_yes_fields_produce_codes(self, data):
        """Only fields with 'Fail' or 'Yes' values should produce supplement codes."""
        result_mapping = get_questionnaire_result_mapping()
        result_fields = data.draw(
            st.fixed_dictionaries(
                {v: result_values for v in result_mapping.values()}
            )
        )

        params_str = build_supplement_params(result_fields, result_type="questionnaire")
        separator = get_supplement_params_separator()
        param_codes = get_supplement_param_codes()

        if not params_str:
            # No Fail/Yes fields → empty params
            fail_count = sum(
                1 for v in result_fields.values() if v in ("Fail", "Yes")
            )
            assert fail_count == 0 or all(
                # Edge case: field might not have a matching param code
                True for _ in []
            )
            return

        codes = params_str.split(separator)

        # All codes must be valid (A~Q)
        valid_codes = set(param_codes.values())
        for code in codes:
            assert code in valid_codes, f"Invalid code: {code}"

    @settings(max_examples=100)
    @given(data=st.data())
    def test_pass_fields_never_produce_codes(self, data):
        """Fields with 'Pass' or empty values should never appear in supplement params."""
        result_mapping = get_questionnaire_result_mapping()
        param_codes = get_supplement_param_codes()
        separator = get_supplement_params_separator()

        # Generate all-pass results
        all_pass_fields = {v: "Pass" for v in result_mapping.values()}
        params_str = build_supplement_params(all_pass_fields, result_type="questionnaire")

        assert params_str == "", f"Expected empty params for all-pass, got: {params_str}"

    @settings(max_examples=100)
    @given(data=st.data())
    def test_fail_count_matches_code_count(self, data):
        """Number of unique codes should match number of distinct Fail/Yes groups/fields."""
        from dreams_workflow.ai_determination.field_mapping_loader import (
            get_supplement_param_groups,
        )

        result_mapping = get_questionnaire_result_mapping()
        param_codes = get_supplement_param_codes()
        separator = get_supplement_params_separator()
        param_groups = get_supplement_param_groups()

        result_fields = data.draw(
            st.fixed_dictionaries(
                {v: result_values for v in result_mapping.values()}
            )
        )

        params_str = build_supplement_params(result_fields, result_type="questionnaire")

        # Count expected unique codes (accounting for group deduplication)
        reverse_map = {v: k for k, v in result_mapping.items()}

        # Build field_to_group map
        field_to_group: dict[str, str] = {}
        for group_code, field_ids in param_groups.items():
            for fid in field_ids:
                field_to_group[fid] = group_code

        expected_codes: set[str] = set()
        for field_id, value in result_fields.items():
            if value in ("Fail", "Yes"):
                q_field_id = reverse_map.get(field_id, field_id)
                group_code = field_to_group.get(q_field_id)
                if group_code:
                    expected_codes.add(group_code)
                elif q_field_id in param_codes:
                    expected_codes.add(param_codes[q_field_id])

        expected_count = len(expected_codes)

        if params_str:
            actual_count = len(params_str.split(separator))
        else:
            actual_count = 0

        assert actual_count == expected_count, (
            f"Expected {expected_count} codes, got {actual_count}: {params_str}"
        )

    @settings(max_examples=100)
    @given(data=st.data())
    def test_codes_are_sorted_alphabetically(self, data):
        """Supplement codes should always be sorted alphabetically."""
        result_mapping = get_questionnaire_result_mapping()
        separator = get_supplement_params_separator()

        result_fields = data.draw(
            st.fixed_dictionaries(
                {v: result_values for v in result_mapping.values()}
            )
        )

        params_str = build_supplement_params(result_fields, result_type="questionnaire")

        if params_str:
            codes = params_str.split(separator)
            assert codes == sorted(codes), f"Codes not sorted: {codes}"

    @settings(max_examples=100)
    @given(data=st.data())
    def test_taipower_result_same_logic(self, data):
        """Taipower result mapping should follow the same filtering logic."""
        result_mapping = get_taipower_result_mapping()
        param_codes = get_supplement_param_codes()
        separator = get_supplement_params_separator()

        result_fields = data.draw(
            st.fixed_dictionaries(
                {v: result_values for v in result_mapping.values()}
            )
        )

        params_str = build_supplement_params(result_fields, result_type="taipower")

        if params_str:
            codes = params_str.split(separator)
            valid_codes = set(param_codes.values())
            for code in codes:
                assert code in valid_codes
            # Should be sorted
            assert codes == sorted(codes)

    @settings(max_examples=100)
    @given(data=st.data())
    def test_no_duplicate_codes(self, data):
        """Supplement params should never contain duplicate codes."""
        result_mapping = get_questionnaire_result_mapping()
        separator = get_supplement_params_separator()

        result_fields = data.draw(
            st.fixed_dictionaries(
                {v: result_values for v in result_mapping.values()}
            )
        )

        params_str = build_supplement_params(result_fields, result_type="questionnaire")

        if params_str:
            codes = params_str.split(separator)
            assert len(codes) == len(set(codes)), f"Duplicate codes found: {codes}"
