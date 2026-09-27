// Application-owned reproduction probe for public cpp-peglib issue #121.
// This is a bounded API exercise, not an exhaustive project test suite.
#include "peglib.h"
#include <iostream>
#include <memory>
#include <string>

int main(int argc, char **argv) {
  if (argc != 2) return 64;
  const std::string mode = argv[1];
  if (mode != "ignored" && mode != "ordinary") return 64;
  const char *grammar = mode == "ignored" ? "~ROOT <- ' '" : "ROOT <- ' '";
  peg::parser parser(grammar);
  const bool accepted = static_cast<bool>(parser);
  std::cout << "{\"phase\":\"grammar\",\"accepted\":"
            << (accepted ? "true" : "false") << "}" << std::endl;
  if (!accepted) return 0;
  parser.enable_ast();
  std::shared_ptr<peg::Ast> ast;
  const bool parsed = parser.parse(" ", ast);
  std::cout << "{\"phase\":\"parsed\",\"parse_ok\":"
            << (parsed ? "true" : "false") << ",\"ast_nonnull\":"
            << (ast ? "true" : "false") << "}" << std::endl;
  if (!parsed) return 0;
  // Exercise the same caller precondition used by the upstream AST workflow.
  ast = peg::AstOptimizer(true).optimize(ast);
  std::cout << "{\"phase\":\"optimized\",\"ast_nonnull\":"
            << (ast ? "true" : "false") << "}" << std::endl;
  return 0;
}
