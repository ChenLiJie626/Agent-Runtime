"""Real Clang analyzer integration for the bundled use-after-free rule."""

import json
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_runtime import (
    AnalysisPipeline,
    ClangUseAfterFreeDiscoverer,
    CppUseAfterFreeEvaluator,
    DefectRuntime,
    EvidenceReviewExecutor,
    FixedSnapshot,
    SQLiteStore,
    analysis_profile_digest,
    cpp_use_after_free_profile,
    cpp_use_after_free_rule,
    digest,
    BackendFailure,
    PolicyDenied,
)
from agent_runtime.adapters import (
    ClangCompilationDatabase,
    ClangDiagnosticProgramQuery,
    CompositeProgramQuery,
    FrozenSourceProgramQuery,
    run_clang_use_after_free_analysis,
)
from agent_runtime.adapters.codechecker import _load_codechecker_diagnostics


class CodeCheckerReportTests(unittest.TestCase):
    def test_codechecker_plist_is_normalized_to_a_ctu_diagnostic(self):
        sources = {
            "src/main.cpp": "int main() { return read(); }\n",
            "src/read.cpp": "int read() { return *expired; }\n",
        }
        with tempfile.TemporaryDirectory(prefix="codechecker-report-") as directory:
            root = Path(directory)
            report = root / "main.cpp_clangsa_test.plist"
            diagnostic = {
                "check_name": "cplusplus.NewDelete",
                "description": "Use of memory after it is freed",
                "issue_hash_content_of_line_in_context": "stable-uaf-hash",
                "location": {"file": 0, "line": 1, "col": 21},
                "path": [{"kind": "event"}, {"kind": "event"}],
            }
            report.write_bytes(plistlib.dumps({
                "files": [
                    "/workspace/source/src/read.cpp",
                    "/workspace/source/src/main.cpp",
                ],
                "diagnostics": [diagnostic],
                "metadata": {"analyzer": {"name": "clangsa"}},
            }))
            duplicate = root / "read.cpp_clangsa_test.plist"
            duplicate.write_bytes(plistlib.dumps({
                "files": [
                    "/workspace/source/src/read.cpp",
                    "/workspace/source/src/main.cpp",
                ],
                "diagnostics": [{**diagnostic, "path": [{"kind": "event"}]}],
                "metadata": {"analyzer": {"name": "clangsa"}},
            }))
            (root / "metadata.json").write_text(json.dumps({
                "tools": [{
                    "result_source_files": {
                        "/workspace/output/reports/" + report.name:
                            "/workspace/source/src/main.cpp",
                        "/workspace/output/reports/" + duplicate.name:
                            "/workspace/source/src/read.cpp",
                    },
                }],
            }), encoding="utf-8")
            diagnostics, analyses = _load_codechecker_diagnostics(
                root, sources, FrozenSourceProgramQuery.source_digest(sources),
            )
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].path, "src/read.cpp")
        self.assertEqual(diagnostics[0].translation_unit, "src/main.cpp")
        self.assertEqual(diagnostics[0].rule_id, "cplusplus.NewDelete")
        self.assertIn("src/main.cpp", analyses)


@unittest.skipUnless(shutil.which("clang++"), "clang++ is required")
class UseAfterFreeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            "src/main.cpp": (
                "#include <cstdlib>\n"
                "int f() {\n"
                "  int* p = static_cast<int*>(std::malloc(sizeof(int)));\n"
                "  if (p == nullptr) return -1;\n"
                "  *p = 7;\n"
                "  std::free(p);\n"
                "  return *p;\n"
                "}\n"
            ),
        }

    def test_clang_capture_normalizes_a_use_after_free_diagnostic(self):
        bundle = run_clang_use_after_free_analysis(self.sources)
        self.assertEqual(len(bundle.diagnostics), 1)
        diagnostic = bundle.diagnostics[0]
        self.assertEqual(diagnostic.path, "src/main.cpp")
        self.assertEqual(diagnostic.line, 7)
        self.assertEqual(diagnostic.rule_id, "unix.Malloc")
        self.assertIn("after it is freed", diagnostic.message)
        self.assertNotIn("agent-runtime-clang-", str(bundle.analyses))

    def test_clang_capture_does_not_report_a_read_before_free(self):
        safe_sources = {
            "src/safe.cpp": (
                "#include <cstdlib>\n"
                "int f() {\n"
                "  int* p = static_cast<int*>(std::malloc(sizeof(int)));\n"
                "  if (p == nullptr) return -1;\n"
                "  *p = 7;\n"
                "  int result = *p;\n"
                "  std::free(p);\n"
                "  return result;\n"
                "}\n"
            ),
        }
        bundle = run_clang_use_after_free_analysis(safe_sources)
        self.assertEqual(bundle.diagnostics, ())

    def test_clang_capture_tracks_header_location_and_translation_unit(self):
        sources = {
            "include/owner.hpp": (
                "#pragma once\n"
                "class Owner { public: Owner(): p(new int(5)) {} "
                "~Owner(){ reset(); } int* get(){ return p; } "
                "void reset(){ delete p; p=nullptr; } private: int* p; };\n"
            ),
            "include/view.hpp": (
                "#pragma once\n"
                "class View { public: explicit View(int* p): p_(p) {}\n"
                "int read() const { return *p_; }\n"
                "private: int* p_; };\n"
            ),
            "include/reader.hpp": (
                "#pragma once\n#include \"view.hpp\"\n"
                "class Reader { public: int consume(const View& v) const "
                "{ return v.read(); } };\n"
            ),
            "src/workflow.cpp": (
                "#include \"owner.hpp\"\n#include \"reader.hpp\"\n"
                "int workflow(){ Owner o; View v(o.get()); Reader r; "
                "o.reset(); return r.consume(v); }\n"
            ),
        }
        bundle = run_clang_use_after_free_analysis(sources)
        self.assertEqual(len(bundle.diagnostics), 1)
        diagnostic = bundle.diagnostics[0]
        self.assertEqual(diagnostic.path, "include/view.hpp")
        self.assertEqual(diagnostic.translation_unit, "src/workflow.cpp")
        self.assertEqual(diagnostic.rule_id, "cplusplus.NewDelete")

    def test_compile_database_flags_are_relocated_and_replayed(self):
        with tempfile.TemporaryDirectory(prefix="compile-db-test-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            sources = {
                "include/owner.hpp": (
                    "#pragma once\nclass Owner { public: Owner(): p(new int(3)) {} "
                    "void reset(){ delete p; } int* get(){ return p; } "
                    "private: int* p; };\n"
                ),
                "src/main.cpp": (
                    "#include \"owner.hpp\"\nint main(){ Owner o; auto p=o.get(); "
                    "o.reset(); return *p; }\n"
                ),
            }
            database = ClangCompilationDatabase.from_json([{
                "directory": str(project / "build"),
                "file": str(project / "src/main.cpp"),
                "arguments": [
                    "/usr/bin/clang++", "-I", str(project / "include"),
                    "-std=c++17", "-DFIXTURE_BUILD=1", "-o", "main.o", "-c",
                    str(project / "src/main.cpp"),
                ],
            }], project_root=project)
            bundle = run_clang_use_after_free_analysis(
                sources,
                compiler="/usr/bin/clang++",
                compilation_database=database,
            )
        self.assertEqual(len(bundle.diagnostics), 1)
        self.assertEqual(bundle.compilation_database_digest, database.database_digest)
        command = bundle.analyses["src/main.cpp"]["command"]
        self.assertIn("-DFIXTURE_BUILD=1", command)
        self.assertIn("$SNAPSHOT_ROOT/include", command)
        self.assertNotIn("main.o", command)

    def test_compile_database_rejects_plugins_and_direct_ctu_claims(self):
        with tempfile.TemporaryDirectory(prefix="compile-db-policy-") as directory:
            project = Path(directory)
            database = ClangCompilationDatabase.from_json([{
                "directory": str(project),
                "file": str(project / "main.cpp"),
                "arguments": ["clang++", "-fplugin=/tmp/plugin.so", "-c", "main.cpp"],
            }], project_root=project)
            with self.assertRaises(PolicyDenied):
                database.analyzer_arguments("main.cpp", project)
        with self.assertRaises(BackendFailure):
            run_clang_use_after_free_analysis(self.sources, ctu_mode="codechecker")

    def test_runtime_pipeline_confirms_the_clang_candidate(self):
        bundle = run_clang_use_after_free_analysis(self.sources)
        rule = cpp_use_after_free_rule()
        policy = digest({"operations": ["read_source", "read_clang_diagnostic"]})
        profile = cpp_use_after_free_profile(policy)
        snapshot = FixedSnapshot(
            "fixture/uaf",
            "full-project",
            FrozenSourceProgramQuery.source_digest(self.sources),
            analysis_profile_digest(rule, profile=profile),
            policy,
        )
        backend = CompositeProgramQuery(
            FrozenSourceProgramQuery(snapshot, self.sources),
            ClangDiagnosticProgramQuery(snapshot, bundle),
        )
        with tempfile.TemporaryDirectory(prefix="runtime-uaf-test-") as directory:
            root = Path(directory)
            store = SQLiteStore(root / "runtime.sqlite3", root / "artifacts")
            try:
                result = AnalysisPipeline(
                    DefectRuntime(store),
                    rule=rule,
                    profile=profile,
                    discoverer=ClangUseAfterFreeDiscoverer(bundle),
                    backend=backend,
                    evaluator=CppUseAfterFreeEvaluator(),
                    executor=EvidenceReviewExecutor(),
                    owner_id="test-worker",
                    working_dir=directory,
                ).run(snapshot, base_sources=self.sources, head_sources=self.sources)
            finally:
                store.close()
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].status, "confirmed")
        self.assertEqual(result.candidates[0].material["rule_id"], "unix.Malloc")
        self.assertEqual(len(result.candidates[0].decision.support_refs), 2)


if __name__ == "__main__":
    unittest.main()
