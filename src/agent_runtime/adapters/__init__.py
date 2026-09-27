"""Optional execution and program-query adapters."""

from .claude import ClaudeAgentExecutor, ClaudeConfig
from .clang import (
    ClangAnalysisBundle,
    ClangDiagnostic,
    ClangDiagnosticProgramQuery,
    run_clang_use_after_free_analysis,
)
from .composite import CompositeProgramQuery
from .codeql import (
    CodeQLCaptureBundle,
    CodeQLReplayConfig,
    CodeQLReplayProgramQuery,
)
from .compilation_database import ClangCompilationDatabase, CompilationCommand
from .docker_validation import (
    DockerAttemptEvidence,
    DockerOutputArtifact,
    DockerValidationAttempt,
    DockerValidationExecutor,
    DockerValidationLimits,
    DockerValidationSuite,
    load_docker_validation_suite,
)
from .codechecker import (
    CodeCheckerCtuConfig,
    run_codechecker_ctu_use_after_free_analysis,
)
from .joern import JoernConfig, JoernProgramQuery
from .layered_ctu import (
    AnalysisLayer,
    IncludeChain,
    LayeredCtuPlan,
    LayeredCtuPlanner,
    TargetSelection,
)
from .recorded import RecordedJoernProgramQuery
from .source import FrozenSourceProgramQuery
from .validation import RecordedValidationProgramQuery

__all__ = [
    "ClaudeAgentExecutor", "ClaudeConfig", "CompositeProgramQuery",
    "ClangAnalysisBundle", "ClangDiagnostic", "ClangDiagnosticProgramQuery",
    "ClangCompilationDatabase", "CompilationCommand",
    "CodeCheckerCtuConfig", "run_codechecker_ctu_use_after_free_analysis",
    "CodeQLCaptureBundle", "CodeQLReplayConfig", "CodeQLReplayProgramQuery",
    "DockerAttemptEvidence", "DockerOutputArtifact", "DockerValidationAttempt", "DockerValidationExecutor",
    "DockerValidationLimits", "DockerValidationSuite", "load_docker_validation_suite",
    "JoernConfig", "JoernProgramQuery", "RecordedJoernProgramQuery",
    "RecordedValidationProgramQuery",
    "AnalysisLayer", "IncludeChain", "LayeredCtuPlan", "LayeredCtuPlanner",
    "TargetSelection",
    "FrozenSourceProgramQuery", "run_clang_use_after_free_analysis",
]
