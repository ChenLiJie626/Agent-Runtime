/**
 * @name Potential access after delete candidate
 * @description Finds a same-function pointer access textually after a delete.
 *              This is a bounded candidate signal, not a defect verdict.
 * @kind problem
 * @problem.severity warning
 * @precision low
 * @id cpp/potential-access-after-delete-candidate
 */
import cpp

from DeleteExpr deletion, VariableAccess deletedAccess, VariableAccess laterAccess
where
  deletedAccess = deletion.getExpr() and
  laterAccess.getTarget() = deletedAccess.getTarget() and
  laterAccess.getEnclosingFunction() = deletion.getEnclosingFunction() and
  laterAccess.getLocation().getStartLine() > deletion.getLocation().getStartLine()
select laterAccess,
  "Pointer is accessed after a delete expression in the same function; inspect lifetime and control flow."
