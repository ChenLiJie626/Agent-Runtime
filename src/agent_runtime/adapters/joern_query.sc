// Fixed operation allowlist. Callers may supply only a simple symbol name.
@main def exec(cpgFile: String, operation: String, symbol: String, callLine: Int, contextSymbol: String, sourcePath: String, returnLine: Int, followingLine: Int, maxResults: Int) = {
  importCpg(cpgFile)
  println("AGENT_RUNTIME_QUERY_BEGIN")
  if (operation == "get_function") {
    println(cpg.method.name(symbol).toJsonPretty)
  } else if (operation == "find_callers") {
    println(cpg.method.name(symbol).callIn.toJsonPretty)
  } else if (operation == "get_guards") {
    val structures = cpg.method.name(symbol).controlStructure.toJsonPretty
    val trueBranch = cpg.method.name(symbol).controlStructure.whenTrue.ast.toJsonPretty
    val controllers = cpg.method.name(symbol).ast.isCall
      .name("<operator>.indirection").controlledBy.toJsonPretty
    val dominators = cpg.method.name(symbol).ast.isCall
      .name("<operator>.indirection").dominatedBy.toJsonPretty
    println(
      s"""[{"kind":"control_structures","nodes":$structures},""" +
      s"""{"kind":"true_branch_ast","nodes":$trueBranch},""" +
      s"""{"kind":"dereference_controllers","nodes":$controllers},""" +
      s"""{"kind":"dereference_dominators","nodes":$dominators}]"""
    )
  } else if (operation == "map_arguments") {
    val calls = cpg.call.name(symbol).filter(_.lineNumber.contains(callLine)).toJsonPretty
    val arguments = cpg.call.name(symbol).filter(_.lineNumber.contains(callLine))
      .argument.toJsonPretty
    val parameters = cpg.method.name(symbol).parameter.toJsonPretty
    println(
      s"""[{"kind":"candidate_calls","nodes":$calls},""" +
      s"""{"kind":"actual_arguments","nodes":$arguments},""" +
      s"""{"kind":"formal_parameters","nodes":$parameters}]"""
    )
  } else if (operation == "trace_value") {
    val sink = cpg.method.name(contextSymbol).ast.isCall
      .name("<operator>.indirection").argument
    val source = cpg.method.name(contextSymbol).ast.isCall.name(symbol)
    println(sink.reachableByFlows(source).take(maxResults).p)
  } else if (operation == "check_reachability") {
    val sink = cpg.call.name(symbol).argument
    val source = cpg.method.name(contextSymbol).ast.isLiteral.code("0")
    println(sink.reachableByFlows(source).take(maxResults).p)
  } else if (operation == "inspect_unreachable_after_return") {
    val methods = cpg.method.nameExact(symbol).filter(_.filename == sourcePath).toJsonPretty
    val returns = cpg.ret.filter(_.lineNumber.contains(returnLine))
      .where(_.method.nameExact(symbol).filter(_.filename == sourcePath)).toJsonPretty
    val following = cpg.method.nameExact(symbol).filter(_.filename == sourcePath)
      .cfgNode.filter(_.lineNumber.contains(followingLine)).toJsonPretty
    val returnAncestors = cpg.ret.filter(_.lineNumber.contains(returnLine))
      .where(_.method.nameExact(symbol).filter(_.filename == sourcePath))
      .repeat(_.cfgPrev)(_.emit).take(maxResults).toJsonPretty
    val followingAncestors = cpg.method.nameExact(symbol).filter(_.filename == sourcePath)
      .cfgNode.filter(_.lineNumber.contains(followingLine))
      .repeat(_.cfgPrev)(_.emit).take(maxResults).toJsonPretty
    println(
      s"""[{"kind":"candidate_methods","nodes":$methods},""" +
      s"""{"kind":"return_nodes","nodes":$returns},""" +
      s"""{"kind":"following_nodes","nodes":$following},""" +
      s"""{"kind":"return_cfg_ancestors","nodes":$returnAncestors},""" +
      s"""{"kind":"following_cfg_ancestors","nodes":$followingAncestors}]"""
    )
  } else {
    throw new IllegalArgumentException("unsupported operation")
  }
  println("AGENT_RUNTIME_QUERY_END")
}
