from __future__ import annotations

import math
import unittest

from astrbot_ex.core.actions import models


class JsonBudgetTests(unittest.TestCase):
    def test_shared_acyclic_subtrees_are_allowed(self) -> None:
        shared = {"value": 1}
        self.assertIsNone(models.measure_json_budget([shared, shared]))
        self.assertIsNone(models.find_non_finite([shared, shared]))

    def test_cycles_are_rejected(self) -> None:
        value: list[object] = []
        value.append(value)

        self.assertEqual(
            models.measure_json_budget(value),
            models.ErrorCode.INVALID_TYPE,
        )
        error = models.find_non_finite(value)
        self.assertIsNotNone(error)
        self.assertEqual(error.code, models.ErrorCode.INVALID_TYPE)

    def test_non_finite_values_are_rejected(self) -> None:
        self.assertEqual(
            models.measure_json_budget({"value": math.nan}),
            models.ErrorCode.NON_FINITE_NUMBER,
        )
        error = models.find_non_finite({"value": math.inf})
        self.assertIsNotNone(error)
        self.assertEqual(error.code, models.ErrorCode.NON_FINITE_NUMBER)

    def test_depth_node_and_byte_limits_are_bounded(self) -> None:
        nested: object = {}
        for _ in range(models.MAX_VALIDATION_DEPTH + 1):
            nested = {"next": nested}
        self.assertEqual(
            models.measure_json_budget(nested),
            models.ErrorCode.SCHEMA_DEPTH_EXCEEDED,
        )

        too_many_nodes = [0] * models.MAX_VALIDATION_NODES
        self.assertEqual(
            models.measure_json_budget(too_many_nodes),
            models.ErrorCode.VALUE_BUDGET_EXCEEDED,
        )

        too_many_bytes = {"text": "x" * models.MAX_VALIDATION_BYTES}
        self.assertEqual(
            models.measure_json_budget(too_many_bytes),
            models.ErrorCode.VALUE_BUDGET_EXCEEDED,
        )


class SchemaValidationTests(unittest.TestCase):
    def test_explicit_null_keywords_are_rejected(self) -> None:
        keywords = (
            "properties",
            "required",
            "additionalProperties",
            "enum",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "pattern",
            "format",
            "items",
        )
        for keyword in keywords:
            errors = models.check_schema_supported(
                {"type": "object", keyword: None}
            )
            self.assertTrue(errors, keyword)
            self.assertEqual(errors[0].code, models.ErrorCode.INVALID_TYPE, keyword)

        self.assertEqual(
            models.check_schema_supported(
                {"type": "object", "properties": {}}
            ),
            [],
        )

    def test_required_items_are_structured_errors(self) -> None:
        errors = models.check_schema_supported(
            {"type": "object", "required": [{}]}
        )
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, models.ErrorCode.INVALID_TYPE)

    def test_numeric_bounds_are_checked_without_integer_float_coercion(self) -> None:
        huge = 10**3000
        self.assertEqual(
            models.check_schema_supported(
                {"type": "number", "minimum": huge, "maximum": huge - 1}
            )[0].code,
            models.ErrorCode.SCHEMA_BOUNDS_CONFLICT,
        )

    def test_multiple_of_uses_exact_decimal_values_and_integer_modulo(self) -> None:
        for step, accepted, rejected in (
            (1, 1.0, 1.0000000001),
            (0.1, 0.3, 0.3000000001),
            (2, 9007199254740994, 9007199254740993),
        ):
            schema = {"type": "object", "properties": {"x": {"type": "number", "multipleOf": step}}}
            with self.subTest(step=step):
                self.assertEqual(models.validate_params(schema, {"x": accepted}), [])
                errors = models.validate_params(schema, {"x": rejected})
                self.assertEqual([(e.code, e.path) for e in errors], [(models.ErrorCode.RANGE_VIOLATION, "params.x")])

    def test_effective_open_bounds_do_not_discard_closed_endpoint(self) -> None:
        for bounds, accepted in (
            ({"minimum": 10, "exclusiveMinimum": 0, "maximum": 10}, 10),
            ({"minimum": 10, "maximum": 10, "exclusiveMaximum": 20}, 10),
        ):
            schema = {"type": "object", "properties": {"x": {"type": "number", **bounds}}}
            with self.subTest(bounds=bounds):
                self.assertEqual(models.check_schema_supported(schema), [])
                self.assertEqual(models.validate_params(schema, {"x": accepted}), [])
        for bounds in (
            {"minimum": 10, "exclusiveMinimum": 10, "maximum": 10},
            {"minimum": 10, "maximum": 10, "exclusiveMaximum": 10},
        ):
            schema = {"type": "object", "properties": {"x": {"type": "number", **bounds}}}
            self.assertEqual(models.check_schema_supported(schema)[0].code, models.ErrorCode.SCHEMA_BOUNDS_CONFLICT)

    def test_typeless_enum_cannot_silently_ignore_constraints(self) -> None:
        for child, value in (
            ({"enum": [0], "minimum": 1}, 0),
            ({"enum": ["x"], "minLength": 3}, "x"),
            ({"enum": [[1]], "minItems": 2}, [1]),
            ({"enum": [{"value": 1}], "required": ["missing"]}, {"value": 1}),
            ({"enum": [{"value": 1}], "properties": {"value": {"type": "integer"}}}, {"value": 1}),
            ({"enum": [[1]], "items": {"type": "integer"}}, [1]),
            ({"enum": ["x"], "pattern": "^x$"}, "x"),
            ({"enum": ["x"], "format": "uuid"}, "x"),
            ({"enum": [[1]], "uniqueItems": True}, [1]),
            ({"enum": [{"value": 1}], "additionalProperties": False}, {"value": 1}),
        ):
            schema = {"type": "object", "properties": {"x": child}}
            with self.subTest(child=child):
                errors = models.check_schema_supported(schema)
                self.assertTrue(any(error.code == models.ErrorCode.SCHEMA_UNSUPPORTED_KEYWORD for error in errors))
                self.assertTrue(models.validate_params(schema, {"x": value}))
        enum_only = {"type": "object", "properties": {"x": {"title": "Selected", "description": "A number", "enum": [0]}}}
        self.assertEqual(models.validate_params(enum_only, {"x": 0}), [])
        self.assertEqual(models.validate_params(enum_only, {"x": True})[0].code, models.ErrorCode.ENUM_VIOLATION)

    def test_extreme_multiple_of_uses_exact_decimal_arithmetic(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "number", "multipleOf": 1e-300}}}
        self.assertEqual(models.validate_params(schema, {"x": 1e300}), [])
        errors = models.validate_params(schema, {"x": 1e-320})
        self.assertEqual([(error.code, error.path) for error in errors], [(models.ErrorCode.RANGE_VIOLATION, "params.x")])

    def test_large_integer_and_subnormal_multiple_of(self) -> None:
        for huge in (10**500, 10**3000):
            for step, accepted, rejected in (
                (1.0, huge, 1.0000000001),
                (2.0, huge, huge + 1),
                (0.5, huge, 0.3),
            ):
                schema = {"type": "object", "properties": {"x": {"type": "number", "multipleOf": step}}}
                with self.subTest(step=step, digits=len(str(huge))):
                    self.assertEqual(models.validate_params(schema, {"x": accepted}), [])
                    errors = models.validate_params(schema, {"x": rejected})
                    self.assertEqual([(error.code, error.path) for error in errors], [(models.ErrorCode.RANGE_VIOLATION, "params.x")])
        for step, accepted, rejected in (
            (1e-320, 3e-320, 3.1e-320),
            (1e-323, 2e-323, 1.5e-323),
        ):
            schema = {"type": "object", "properties": {"x": {"type": "number", "multipleOf": step}}}
            with self.subTest(step=step):
                self.assertEqual(models.validate_params(schema, {"x": accepted}), [])
                errors = models.validate_params(schema, {"x": rejected})
                self.assertEqual([(error.code, error.path) for error in errors], [(models.ErrorCode.RANGE_VIOLATION, "params.x")])


class EnumValidationTests(unittest.TestCase):
    def test_nested_enum_distinguishes_bool_from_number(self) -> None:
        schema = {
            "type": "object",
            "properties": {"x": {"enum": [[1]]}},
        }
        errors = models.validate_params(schema, {"x": [True]})
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, models.ErrorCode.ENUM_VIOLATION)

    def test_nested_enum_accepts_numeric_int_float_equality(self) -> None:
        schema = {
            "type": "object",
            "properties": {"x": {"enum": [[1]]}},
        }
        self.assertEqual(models.validate_params(schema, {"x": [1.0]}), [])

        scalar_schema = {
            "type": "object",
            "properties": {"x": {"enum": [1]}},
        }
        self.assertEqual(models.validate_params(scalar_schema, {"x": 1.0}), [])

    def test_shared_aliases_in_enum_compare_by_active_path(self) -> None:
        allowed_child = {"value": 1}
        actual_child = {"value": 1}
        schema = {
            "type": "object",
            "properties": {
                "x": {
                    "enum": [[allowed_child, allowed_child]],
                }
            },
        }
        self.assertEqual(
            models.validate_params(
                schema,
                {"x": [actual_child, actual_child]},
            ),
            [],
        )

    def test_huge_integer_enum_is_valid_json_and_does_not_call_isfinite_on_int(self) -> None:
        huge = 10**3000
        schema = {
            "type": "object",
            "properties": {"x": {"enum": [huge]}},
        }
        self.assertEqual(models.validate_params(schema, {"x": huge}), [])


class RevisionValidationTests(unittest.TestCase):
    def test_missing_expected_revision_still_advances(self) -> None:
        self.assertEqual(models.check_revision(None, 4), 5)

    def test_current_revision_rejects_bool(self) -> None:
        with self.assertRaises(models.ContractError):
            models.check_revision(None, True)

    def test_revision_upper_bound_and_increment_overflow_are_rejected(self) -> None:
        with self.assertRaises(models.ContractError):
            models.check_revision(None, models.MAX_SEQUENCE)

        self.assertEqual(
            models.check_revision(
                models.MAX_SEQUENCE - 1,
                models.MAX_SEQUENCE - 1,
            ),
            models.MAX_SEQUENCE,
        )
