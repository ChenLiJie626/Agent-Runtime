"""Tests for syntax-only lifetime candidates and ASan evidence normalization."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_runtime.lifetime_candidates import (
    ASYNC_MEMBER_DESTRUCTION_RULE,
    CONTAINER_INVALIDATION_RULE,
    COROUTINE_DESTRUCTION_RULE,
    RETURNED_RESOURCE_RULE,
    STACK_CONTEXT_ESCAPE_RULE,
    VECTOR_FIELD_COPY_RULE,
    LifetimeCandidate,
    build_asan_checklist,
    normalize_asan_result,
    scan_cpp_source,
)


class LifetimeCandidateTests(unittest.TestCase):
    def test_container_mutation_after_borrow_and_reuse(self):
        source = """\
#include <vector>
void append(std::vector<int>& values) {
  const auto& first = values[0];
  values.push_back(42);
  consume(first);
}
"""
        candidates = scan_cpp_source("src/container.cpp", source)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.rule_id, CONTAINER_INVALIDATION_RULE)
        self.assertEqual(candidate.line, 3)
        self.assertIn("values.push_back", candidate.reason)
        self.assertEqual(len(candidate.evidence_hints), 3)

    def test_container_borrow_without_later_use_is_not_reported(self):
        source = """\
void append(std::vector<int>& values) {
  auto iterator = values.begin();
  consume(*iterator);
  values.push_back(42);
}
"""
        self.assertEqual(scan_cpp_source("src/safe.cpp", source), ())

    def test_container_explicit_pointer_borrow_is_reported(self):
        source = """\
void append(std::vector<int>& values) {
  int* first = values.data();
  values.reserve(values.size() + 100);
  consume(*first);
}
"""
        candidates = scan_cpp_source("src/pointer.cpp", source)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].rule_id, CONTAINER_INVALIDATION_RULE)
        self.assertIn("values.reserve", candidates[0].reason)

    def test_qlever_coroutine_fragment_reports_reverse_destruction(self):
        # Fragment from QLever's pre-fix CompressorStream.h (PR #2812).
        source = """\
template <typename Range>
cppcoro::generator<std::string> compressStream(
    Range range, CompressionMethod compressionMethod) {
  io::filtering_ostream filteringStream;
  std::string stringBuffer;

  filteringStream.push(io::back_inserter(stringBuffer), 0);
  for (const auto& value : range) {
    filteringStream << value;
    co_yield stringBuffer;
  }
}
"""
        candidates = scan_cpp_source("src/util/CompressorStream.h", source)
        candidate = next(
            item for item in candidates if item.rule_id == COROUTINE_DESTRUCTION_RULE
        )
        self.assertEqual(candidate.line, 4)
        self.assertIn("reverse local destruction", candidate.reason)
        self.assertIn("stringBuffer", candidate.reason)

    def test_qlever_async_member_fragment_reports_member_at_risk(self):
        # Reduced from QLever's pre-fix CompressedExternalIdTable.h (PR #2812).
        source = """\
class CompressedExternalIdTableBase {
  Writer writer_;
  std::future<void> compressAndWriteFuture_;
  [[no_unique_address]] BlockTransformation blockTransformation_{};

  void setFuture(std::future<void> future) {
    compressAndWriteFuture_ = std::move(future);
  }

  void pushBlock(Block block) {
    setFuture(std::async(
        std::launch::async, [block = std::move(block), this]() mutable {
          blockTransformation_(block);
          this->writer_.write(std::move(block));
        }));
  }
};
"""
        candidates = scan_cpp_source(
            "src/engine/idTable/CompressedExternalIdTable.h", source
        )
        candidate = next(
            item for item in candidates if item.rule_id == ASYNC_MEMBER_DESTRUCTION_RULE
        )
        self.assertEqual(candidate.line, 4)
        self.assertIn("blockTransformation_", candidate.reason)
        self.assertIn("compressAndWriteFuture_", candidate.reason)

    def test_destructor_wait_suppresses_async_member_candidate(self):
        source = """\
class Owner {
  std::future<void> future_;
  Callback callback_;
 public:
  ~Owner() { future_.wait(); }
  void start() {
    future_ = std::async(std::launch::async, [this] { callback_(); });
  }
};
"""
        self.assertEqual(scan_cpp_source("src/owner.h", source), ())


class AdditionalLifetimeRuleTests(unittest.TestCase):
    def fixture(self, name):
        return (
            Path(__file__).resolve().parents[1]
            / "evaluation/fixtures/lifetime-candidates"
            / name
        ).read_text()

    def test_returned_custom_value_needs_resource_owner_review(self):
        found = scan_cpp_source("value.cpp", self.fixture("returned-resource.cpp"))
        found = [x for x in found if x.rule_id == RETURNED_RESOURCE_RULE]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].line, 8)

    def test_value_copy_ownership_and_explicit_retention_suppress(self):
        source = self.fixture("returned-resource.cpp")
        for safe in (
            source.replace("ValueTable result;", "std::vector<int> result;"),
            source.replace(
                "return result;", "result.retainOwner(blocks); return result;"
            ),
            source.replace("block.asView()", "block.copyValues()"),
        ):
            self.assertFalse(
                any(
                    x.rule_id == RETURNED_RESOURCE_RULE
                    for x in scan_cpp_source("safe.cpp", safe)
                )
            )

    def test_stack_context_lambda_escapes(self):
        found = scan_cpp_source("task.cpp", self.fixture("stack-context.cpp"))
        self.assertEqual([x.rule_id for x in found], [STACK_CONTEXT_ESCAPE_RULE])
        self.assertEqual(found[0].line, 2)

    def test_inline_async_borrow_and_constructor_ref_escape(self):
        for source in (
            "auto start() {\n  Context state;\n  return makeTask(&state);\n}",
            "auto start(Context& source) {\n  Context wrapper(std::ref(source));\n  return std::make_tuple(std::move(wrapper));\n}",
        ):
            self.assertTrue(
                any(
                    x.rule_id == STACK_CONTEXT_ESCAPE_RULE
                    for x in scan_cpp_source("task.cpp", source)
                )
            )

    def test_owning_capture_and_non_escaping_context_do_not_trigger(self):
        source = self.fixture("stack-context.cpp")
        for safe in (
            source.replace("[&context]", "[context]"),
            source.replace("return task;", "task();"),
            source.replace("return task;", "return [] {};"),
        ):
            self.assertFalse(
                any(
                    x.rule_id == STACK_CONTEXT_ESCAPE_RULE
                    for x in scan_cpp_source("safe.cpp", safe)
                )
            )

    def test_indirect_vector_allocator_field_copy(self):
        source = self.fixture("vector-field-copy.cpp")
        found = [
            x
            for x in scan_cpp_source("arena.cpp", source)
            if x.rule_id == VECTOR_FIELD_COPY_RULE
        ]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].line, 12)
        renamed = (
            source.replace("acquire", "obtain")
            .replace("storage", "items")
            .replace("old", "prior")
        )
        self.assertEqual(
            len(
                [
                    x
                    for x in scan_cpp_source("other.cpp", renamed)
                    if x.rule_id == VECTOR_FIELD_COPY_RULE
                ]
            ),
            1,
        )

    def test_refreshed_pointer_and_stable_storage_do_not_trigger(self):
        source = self.fixture("vector-field-copy.cpp")
        safe = source.replace(
            "next->field = old->field;", "old = next; next->field = old->field;"
        )
        self.assertFalse(
            any(
                x.rule_id == VECTOR_FIELD_COPY_RULE
                for x in scan_cpp_source("safe.cpp", safe)
            )
        )
        stable = source.replace("storage.resize(index + 1);", "// no growth")
        self.assertFalse(
            any(
                x.rule_id == VECTOR_FIELD_COPY_RULE
                for x in scan_cpp_source("stable.cpp", stable)
            )
        )

    def test_direct_vector_field_copy(self):
        source = "void copy() {\n  std::vector<Node> nodes;\n  Node* prior = &nodes[0];\n  nodes.resize(100);\n  consume(prior->field);\n}"
        self.assertTrue(
            any(
                x.rule_id == VECTOR_FIELD_COPY_RULE
                for x in scan_cpp_source("copy.cpp", source)
            )
        )

    def test_comments_and_literals_cannot_invent_new_escapes(self):
        source = 'void f() {\n  Context state;\n  const char* text = "return makeTask(&state);";\n  // return makeTask(&state);\n}'
        self.assertEqual(scan_cpp_source("safe.cpp", source), ())

    def test_boolean_and_sync_lambda_and_class_member_are_not_escapes(self):
        for source in (
            "bool f() {\n  bool state = true;\n  return state && state;\n}",
            "bool f() {\n  Context state;\n  return all_of(values, [&state](auto x) { return state.check(x); });\n}",
            "struct Owner {\n  Context state;\n  auto get() { return makeTask(&state); }\n};",
        ):
            self.assertFalse(
                any(
                    x.rule_id == STACK_CONTEXT_ESCAPE_RULE
                    for x in scan_cpp_source("safe.cpp", source)
                )
            )

    def test_scopes_do_not_mix_borrow_and_unrelated_return(self):
        source = (
            "void f() {\n  Context state;\n}\nauto g() { return makeTask(&state); }"
        )
        self.assertFalse(
            any(
                x.rule_id == STACK_CONTEXT_ESCAPE_RULE
                for x in scan_cpp_source("safe.cpp", source)
            )
        )


class AsanEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.candidate = LifetimeCandidate(
            rule_id=CONTAINER_INVALIDATION_RULE,
            path="src/container.cpp",
            line=3,
            reason="borrow may be invalidated",
            evidence_hints=("exercise it",),
        )

    def test_checklist_records_commands_and_requirements(self):
        checklist = build_asan_checklist(
            self.candidate,
            build_command="cmake --build build-asan",
            reproduce_command="build-asan/repro",
        )
        self.assertEqual(checklist.reproduce_command, "build-asan/repro")
        self.assertIn("-fsanitize=address", checklist.required_compile_flags)
        self.assertTrue(
            any("container.cpp" in item for item in checklist.acceptance_checks)
        )

    def test_matching_asan_uaf_confirms_candidate(self):
        log = """\
ERROR: AddressSanitizer: heap-use-after-free on address 0x123
    #0 0x123 in append src/container.cpp:5:10
SUMMARY: AddressSanitizer: heap-use-after-free src/container.cpp:5:10 in append
"""
        result = normalize_asan_result(
            self.candidate,
            command="build-asan/repro",
            exit_code=1,
            stderr=log,
            instrumented=True,
            exercised=True,
        )
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.sanitizer_finding, "heap-use-after-free")
        self.assertTrue(result.matched_frames)

    def test_unrelated_asan_uaf_is_inconclusive(self):
        result = normalize_asan_result(
            self.candidate,
            command="build-asan/repro",
            exit_code=1,
            stderr=(
                "ERROR: AddressSanitizer: heap-use-after-free\n"
                "#0 other/project.cpp:8\n"
            ),
            instrumented=True,
            exercised=True,
        )
        self.assertEqual(result.status, "inconclusive")

    def test_filename_in_command_or_compiler_error_is_not_a_frame(self):
        result = normalize_asan_result(
            self.candidate,
            command="repro",
            exit_code=1,
            stderr="src/container.cpp:10: error\nERROR: AddressSanitizer: heap-use-after-free\n#0 unrelated.cpp:3",
            instrumented=True,
            exercised=True,
        )
        self.assertEqual(result.status, "inconclusive")

    def test_same_basename_in_other_directory_is_not_a_match(self):
        result = normalize_asan_result(
            self.candidate,
            command="repro",
            exit_code=1,
            stderr="ERROR: AddressSanitizer: heap-use-after-free\n#0 /other/container.cpp:3",
            instrumented=True,
            exercised=True,
        )
        self.assertEqual(result.status, "inconclusive")

    def test_frame_in_another_error_block_does_not_link_uaf(self):
        result = normalize_asan_result(
            self.candidate,
            command="repro",
            exit_code=1,
            stderr="ERROR: AddressSanitizer: heap-use-after-free\n#0 other.cpp:3\nERROR: AddressSanitizer: heap-buffer-overflow\n#0 src/container.cpp:3",
            instrumented=True,
            exercised=True,
        )
        self.assertEqual(result.status, "inconclusive")

    def test_unverified_instrumentation_or_execution_cannot_confirm(self):
        for instrumented, exercised in ((False, True), (True, False)):
            result = normalize_asan_result(
                self.candidate,
                command="repro",
                exit_code=1,
                stderr="ERROR: AddressSanitizer: heap-use-after-free\n#0 src/container.cpp:5",
                instrumented=instrumented,
                exercised=exercised,
            )
            self.assertEqual(result.status, "inconclusive")

    def test_explicit_source_root_rejects_another_project_with_same_relative_path(self):
        result = normalize_asan_result(
            self.candidate,
            command="repro",
            exit_code=1,
            stderr="ERROR: AddressSanitizer: heap-use-after-free\n#0 /other/project/src/container.cpp:3",
            instrumented=True,
            exercised=True,
            source_roots=("/workspace/source",),
        )
        self.assertEqual(result.status, "inconclusive")

    def test_clean_exercised_asan_run_refutes_only_reproduction(self):
        result = normalize_asan_result(
            self.candidate,
            command="build-asan/repro",
            exit_code=0,
            stdout="TARGET_PATH_EXERCISED",
            instrumented=True,
            exercised=True,
        )
        self.assertEqual(result.status, "refuted")
        self.assertIn("this reproduction only", result.reason)

    def test_clean_but_unexercised_run_is_inconclusive(self):
        result = normalize_asan_result(
            self.candidate,
            command="build-asan/repro",
            exit_code=0,
            instrumented=True,
            exercised=False,
        )
        self.assertEqual(result.status, "inconclusive")
        self.assertIn("target execution", result.reason)


if __name__ == "__main__":
    unittest.main()
